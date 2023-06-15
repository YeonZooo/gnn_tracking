import collections
import logging
from pathlib import Path
from typing import Any, Callable, DefaultDict

import numpy as np
import pandas as pd
import tabulate
import torch
from pytorch_lightning import LightningModule
from pytorch_lightning.cli import LightningCLI
from torch import Tensor
from torch.optim import Adam
from torch_geometric.data import Data

from gnn_tracking.metrics.binary_classification import (
    BinaryClassificationStats,
    get_maximized_bcs,
    roc_auc_score,
)
from gnn_tracking.metrics.losses import loss_weight_type, unpack_loss_returns
from gnn_tracking.postprocessing.clusterscanner import ClusterFctType
from gnn_tracking.utils.loading import TrackingDataModule
from gnn_tracking.utils.log import get_logger
from gnn_tracking.utils.nomenclature import denote_pt


# The following abbreviations are used throughout the code:
# W: edge weights
# B: condensation likelihoods
# H: clustering coordinates
# Y: edge truth labels
# L: hit truth labels
# P: Track parameters
class TCNTrainer(LightningModule):
    def __init__(
        self,
        model: LightningModule,
        loss_functions: dict[str, tuple[LightningModule, loss_weight_type]],
        *,
        lr: Any = 5e-4,
        optimizer: Callable = Adam,
        lr_scheduler: Callable | None = None,
        cluster_functions: dict[str, ClusterFctType] | None = None,
    ):
        """Main trainer class of the condensation network approach.

        Note: Additional (more advanced) settings are goverend by attributes rather
        than init arguments. Take a look at all attributes that do not start with
        ``_``.

        Args:
            model: The model to train
            loss_functions: Dictionary of loss functions and their weights, keyed by
                loss name. The weights can be specified as (1) float (2) list of floats
                (if the loss function returns a list of losses) or (3) dictionary of
                floats (if the loss function returns a dictionary of losses).
            device:
            lr: Learning rate
            optimizer: Optimizer to use (default: Adam): Function. Will be called with
                the model parameters as first positional parameter and with the learning
                rate as keyword argument (``lr``).
            lr_scheduler: Learning rate scheduler. If it needs parameters, apply
                ``functools.partial`` first
            cluster_functions: Dictionary of functions that take the output of the model
                during testing and report additional figures of merits (e.g.,
                clustering)
        """
        super().__init__()
        # self.save_hyperparameters()
        self.logg = get_logger("TCNTrainer", level=logging.DEBUG)
        #: Checkpoints are saved to this directory by default
        self.checkpoint_dir = Path(".")
        self.model = model

        self.loss_functions = loss_functions

        if cluster_functions is None:
            cluster_functions = {}
        self.clustering_functions = cluster_functions

        self.lr = lr
        self._lr_scheduler = lr_scheduler(self.optimizer) if lr_scheduler else None
        #: Where to step the scheduler: Either epoch or batch
        self.lr_scheduler_step = "epoch"

        # Current epoch
        self._epoch = 0

        #: Mapping of cluster function name to best parameter
        self._best_cluster_params: dict[str, dict[str, Any] | None] = {}

        #: Number of batches that are being used for the clustering functions and the
        #: evaluation of the related metrics.
        self.max_batches_for_clustering = 10

        #: pT thresholds that are being used in the evaluation of edge classification
        #: metrics in the test step
        self.ec_eval_pt_thlds = [0.9, 1.5]

        #: Do not run test step after training epoch
        self.skip_test_during_training = False

        # todo: This should rather be read from the model, because it makes only
        #   sense if it actually matches
        #: Threshold for edge classification in test step (does not
        #: affect training)
        self.ec_threshold = 0.5

        self._n_oom_errors_in_a_row = 0

    def evaluate_model(
        self,
        data: Data,
        mask_pids_reco=True,
    ) -> dict[str, Tensor]:
        """Evaluate the model on the data and return a dictionary of outputs

        Args:
            data:
            mask_pids_reco: If True, mask out PIDs for non-reconstructables
        """
        out = self.model(data)

        if mask_pids_reco:
            pid_field = data.particle_id * data.reconstructable.long()
        else:
            pid_field = data.particle_id

        def get_if_defined(key: str) -> None | Tensor:
            try:
                return out.pop(key)
            except KeyError:
                return None

        ec_hit_mask = out.get("ec_hit_mask", torch.full_like(data.pt, True)).bool()
        ec_edge_mask = out.get("ec_edge_mask", torch.full_like(data.y, True)).bool()

        dct = out
        dct.update(
            {
                # -------- model_outputs
                "w": get_if_defined("W"),
                "x": get_if_defined("H"),
                "beta": get_if_defined("B"),
                "pred": get_if_defined("P"),
                "ec_hit_mask": ec_hit_mask,
                "ec_edge_mask": ec_edge_mask,
                # -------- from data
                "y": data.y,
                "particle_id": pid_field,
                # fixme: One of these is wrong
                "track_params": data.pt,
                "pt": data.pt,
                "reconstructable": data.reconstructable.long(),
                "edge_index": data.edge_index,
                "sector": data.sector,
                "node_features": data.x,
                "batch": data.batch,
            }
        )
        return dct

    def get_batch_losses(
        self, model_output: dict[str, Tensor]
    ) -> tuple[Tensor, dict[str, Tensor], dict[str, Tensor | float]]:
        """Calculate the losses for a batch of data

        Args:
            model_output:

        Returns:
            total loss, individual losses, individual weights
        """
        losses = {}
        weights = collections.defaultdict(lambda: 1.0)
        for key, (loss_func, these_weights) in self.loss_functions.items():
            # We need to unpack depending on whether the loss function returns a
            # single value, a list of values, or a dictionary of values.
            losses.update(unpack_loss_returns(key, loss_func(**model_output)))
            weights.update(unpack_loss_returns(key, these_weights))
        total = sum(weights[k] * losses[k] for k in losses)
        if torch.isnan(total):
            raise RuntimeError(
                f"NaN loss encountered in test step. {losses=}, {weights=}."
            )
        return total, losses, weights

    def _log_results(
        self,
        results: dict[str, Tensor | float],
        *,
        header: str = "",
    ) -> None:
        """Log the losses

        Args:
            results:
            header: Header to prepend to the log message

        Returns:
            None
        """
        report_str = header
        report_str += "\n"
        non_error_keys: list[str] = sorted(
            [
                k
                for k in results
                if not k.endswith("_std")
                if self.printed_results_filter(k)
            ]
        )
        values = [results[k] for k in non_error_keys]
        errors = [float(results.get(f"{k}_std", float("nan"))) for k in non_error_keys]
        markers = [
            "-->" if self.highlight_metric(key) else "" for key in non_error_keys
        ]
        annotated_table_items = zip(markers, non_error_keys, values, errors)
        report_str += tabulate.tabulate(
            annotated_table_items,
            tablefmt="simple_outline",
            floatfmt=".5f",
            headers=["", "Metric", "Value", "Std"],
        )
        self.logg.info(report_str)

    # noinspection PyMethodMayBeStatic
    def printed_results_filter(self, key: str) -> bool:
        """Should a metric be printed in the log output?

        This is meant to be overridden by your personal trainer.
        """
        if "_loc_" in key:
            return False
        return True

    # noinspection PyMethodMayBeStatic
    def highlight_metric(self, metric: str) -> bool:
        """Should a metric be highlighted in the log output?"""
        metric = metric.casefold()
        if metric.startswith("tc_"):
            return False
        if "_loc_" in metric:
            return False
        if "0.9" not in metric and "1.5" not in metric:
            return False
        if "double_majority" in metric:
            return True
        if "tpr_eq_tnr" in metric:
            return True
        if "max_mcc" in metric:
            return True
        return False

    def data_preproc(self, data: Data) -> Data:
        return data

    def training_step(self, batch, batch_idx: int) -> Tensor | None:
        """

        Args:
            max_batches:  Only process this many batches per epoch (useful for testing
                to get to the validation step more quickly)

        Returns:
            Dictionary of losses
        """
        try:
            batch = self.data_preproc(batch)
            model_output = self.evaluate_model(batch)
            batch_loss, _, _ = self.get_batch_losses(model_output)
        except RuntimeError as e:
            if "out of memory" in str(e).casefold():
                self._n_oom_errors_in_a_row += 1
                self.logg.warning(
                    "WARNING: ran out of memory (OOM), skipping batch. "
                    "If this happens frequently, decrease the batch size. "
                    "Will abort if we get 10 consecutive OOM errors."
                )
                if self._n_oom_errors_in_a_row > 10:
                    raise
                return None
            else:
                raise
        else:
            self._n_oom_errors_in_a_row = 0
        return batch_loss

    @staticmethod
    def _edge_pt_mask(edge_index: Tensor, pt: Tensor, pt_min=0.0) -> Tensor:
        """Mask edges where BOTH (!) nodes have pt <= pt_min."""
        pt_a = pt[edge_index[0]]
        pt_b = pt[edge_index[1]]
        return (pt_a > pt_min) | (pt_b > pt_min)

    @torch.no_grad()
    def test_step(self, val=True, max_batches: int | None = None) -> dict[str, float]:
        """Test the model on the validation or test set

        Args:
            val: Use validation dataset rather than test dataset
            max_batches: Only process this many batches per epoch (useful for testing)

        Returns:
            Dictionary of metrics
        """
        self.model.eval()

        # We connect part of the data in CPU memory for clustering & evaluation
        cluster_eval_input: DefaultDict[
            str, list[np.ndarray]
        ] = collections.defaultdict(list)

        batch_metrics = collections.defaultdict(list)
        loader = self.val_loader if val else self.test_loader
        assert loader is not None
        for _batch_idx, data in enumerate(loader):
            if max_batches and _batch_idx > max_batches:
                break
            data = data.to(self.device)  # noqa: PLW2901
            data = self.data_preproc(data)
            model_output = self.evaluate_model(
                data,
                mask_pids_reco=False,
            )
            batch_loss, these_batch_losses, loss_weights = self.get_batch_losses(
                model_output
            )

            batch_metrics["total"].append(batch_loss.item())
            for key, value in these_batch_losses.items():
                batch_metrics[key].append(value.item())
                batch_metrics[f"{key}_weighted"].append(
                    value.item() * loss_weights[key]
                )

            for key, value in self.evaluate_ec_metrics(
                model_output,
            ).items():
                batch_metrics[key].append(value)

            # Build up a dictionary of inputs for clustering (note that we need to
            # map the names of the model outputs to the names of the clustering input)
            if (
                self.clustering_functions
                and _batch_idx <= self.max_batches_for_clustering
            ):
                for mo_key, cf_key in ClusterFctType.required_model_outputs.items():
                    cluster_eval_input[cf_key].append(
                        model_output[mo_key].detach().cpu().numpy()
                    )

        # Merge all metrics in one big dictionary
        metrics: dict[str, float] = (
            {k: np.nanmean(v) for k, v in batch_metrics.items()}
            | {
                f"{k}_std": np.nanstd(v, ddof=1).item()
                for k, v in batch_metrics.items()
            }
            | self._evaluate_cluster_metrics(cluster_eval_input)
            | {f"lw_{key}": f[1] for key, f in self.loss_functions.items()}
        )

        self.test_loss.append(pd.DataFrame(metrics, index=[self._epoch]))
        for hook in self._test_hooks:
            hook(self, metrics)
        return metrics

    def _evaluate_cluster_metrics(
        self, cluster_eval_input: dict[str, list[np.ndarray]]
    ) -> dict[str, float]:
        """Perform cluster studies and evaluate corresponding metrics

        Args:
            cluster_eval_input: Dictionary of inputs for clustering collected in
                `single_test_step`

        Returns:
            Dictionary of cluster metrics
        """
        metrics = {}
        for fct_name, fct in self.clustering_functions.items():
            cluster_result = fct(
                **cluster_eval_input,
                epoch=self._epoch,
                start_params=self._best_cluster_params.get(fct_name),
            )
            if cluster_result is None:
                continue
            metrics.update(cluster_result.metrics)
            self._best_cluster_params[fct_name] = cluster_result.best_params
            metrics.update(
                {
                    f"best_{fct_name}_{param}": val
                    for param, val in cluster_result.best_params.items()
                }
            )
        return metrics

    @torch.no_grad()
    def evaluate_ec_metrics_with_pt_thld(
        self, model_output: dict[str, torch.Tensor], pt_min: float, ec_threshold: float
    ) -> dict[str, float]:
        """Evaluate edge classification metrics for a given pt threshold and
        EC threshold.

        Args:
            model_output: Output of the model
            pt_min: pt threshold: We discard all edges where both nodes have
                `pt <= pt_min` before evaluating any metric.
            ec_threshold: EC threshold

        Returns:
            Dictionary of metrics
        """
        edge_pt_mask = self._edge_pt_mask(
            model_output["edge_index"], model_output["pt"], pt_min
        )
        predicted = model_output["w"][edge_pt_mask]
        true = model_output["y"][edge_pt_mask].long()

        bcs = BinaryClassificationStats(
            output=predicted,
            y=true,
            thld=ec_threshold,
        )
        metrics = bcs.get_all() | get_maximized_bcs(output=predicted, y=true)
        metrics["roc_auc"] = roc_auc_score(y_true=true, y_score=predicted)
        for max_fpr in [
            0.001,
            0.01,
            0.1,
        ]:
            metrics[f"roc_auc_{max_fpr}FPR"] = roc_auc_score(
                y_true=true,
                y_score=predicted,
                max_fpr=max_fpr,
            )
        return {denote_pt(k, pt_min): v for k, v in metrics.items()}

    @torch.no_grad()
    def evaluate_ec_metrics(
        self, model_output: dict[str, torch.Tensor], ec_threshold: float | None = None
    ) -> dict[str, float]:
        """Evaluate edge classification metrics for all pt thresholds."""
        if ec_threshold is None:
            ec_threshold = self.ec_threshold
        if model_output["w"] is None:
            return {}
        ret = {}
        for pt_min in self.ec_eval_pt_thlds:
            ret.update(
                self.evaluate_ec_metrics_with_pt_thld(
                    model_output, pt_min, ec_threshold=ec_threshold
                )
            )
        return ret

    # def step(self, *, max_batches: int | None = None) -> dict[str, float]:
    #     """Train one epoch and test
    #
    #     Args:
    #         max_batches: See train_step
    #     """
    #     self._epoch += 1
    #     timer = Timer()
    #     train_losses = self.training_step(max_batches=max_batches)
    #     train_time = timer()
    #     if not self.skip_test_during_training:
    #         test_results = self.test_step(max_batches=max_batches)
    #     else:
    #         test_results = {}
    #     test_time = timer()
    #     results = (
    #         {
    #             "_time_train": train_time,
    #             "_time_test": test_time,
    #         }
    #         | {f"{k}_train": v for k, v in train_losses.items()}
    #         | test_results
    #     )
    #     self._log_results(
    #         results,
    #         header=f"Results {self._epoch}: ",
    #     )
    #     return results
    #
    # def train(self, epochs=1000, max_batches: int | None = None):
    #     """Train the model.
    #
    #     Args:
    #         epochs:
    #         max_batches: See train_step.
    #
    #     Returns:
    #
    #     """
    #     for _ in range(1, epochs + 1):
    #         try:
    #             self.step(max_batches=max_batches)
    #         except KeyboardInterrupt:
    #             self.logg.warning("Keyboard interrupt")
    #             self.save_checkpoint()
    #             raise
    #     self.save_checkpoint()
    #
    # noinspection PyMethodMayBeStatic

    def configure_optimizers(self) -> Any:
        return Adam(self.model.parameters(), lr=self.lr)


def cli_main():
    # noinspection PyUnusedLocal
    cli = LightningCLI(TCNTrainer, datamodule_class=TrackingDataModule)  # noqa F841


if __name__ == "__main__":
    cli_main()
