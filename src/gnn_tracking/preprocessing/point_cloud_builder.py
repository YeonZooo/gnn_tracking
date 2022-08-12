from __future__ import annotations

import os
from os.path import join

import numpy as np
import pandas as pd
import torch
from torch_geometric.data import Data
from trackml.dataset import load_event


#class GraphSectors:
#    def __init__(
#            self,
#            n_sectors,
#            ds=None,
#            di=None
#    ):

class PointCloudBuilder:
    def __init__(
        self,
        outdir: str,
        indir: str,
        n_sectors: int,
        redo=True,
        pixel_only=False,
        sector_di=0.0001,
        sector_ds=1.1,
        feature_names = ["r", "phi", "z", "eta_rz", "u", "v", "layer"],
        feature_scale = np.array([1,1,1,1,1,1,1]),
        measurement_mode=False,
        thld=0.5,
        remove_noise=False
    ):
        self.outdir = outdir
        self.indir = indir
        self.n_sectors = n_sectors
        self.redo = redo
        self.pixel_only = pixel_only
        self.sector_di=sector_di
        self.sector_ds=sector_ds
        self.feature_names = feature_names
        self.feature_scale = feature_scale # !! important
        self.measurement_mode = measurement_mode
        self.thld=thld
        self.stats={}
        self.remove_noise=remove_noise

        suffix = "-hits.csv.gz"
        self.prefixes, self.exists = [], {}
        outfiles = os.listdir(outdir)
        for p in os.listdir(self.indir):
            if str(p).endswith(suffix):
                prefix = str(p).replace(suffix, "")
                evtid = int(prefix[-9:])
                if f"data{evtid}_s0.pt" in outfiles:
                    self.exists[evtid] = True
                else:
                    self.exists[evtid] = False
                self.prefixes.append(join(indir, prefix))

        self.data_list = []

    def calc_eta(self, r, z):
        theta = np.arctan2(r, z)
        return -1.0 * np.log(np.tan(theta / 2.0))

    def restrict_to_pixel(self, hits):
        pixel_barrel = [(8, 2), (8, 4), (8, 6), (8, 8)]
        pixel_LEC = [(7, 14), (7, 12), (7, 10), (7, 8), (7, 6), (7, 4), (7, 2)]
        pixel_REC = [ (9, 2), (9, 4), (9, 6), (9, 8), (9, 10), (9, 12), (9, 14)]
        pixel_layers = pixel_barrel + pixel_REC + pixel_LEC
        n_layers = len(pixel_layers)
        
        # select barrel layers and assign convenient layer number [0-9]
        hit_layer_groups = hits.groupby(["volume_id", "layer_id"])
        hits = pd.concat(
            [hit_layer_groups.get_group(pixel_layers[i]).assign(layer=i) 
             for i in range(n_layers)]
        )
        return hits

    def append_features(self, hits, particles, truth):
        particles["pt"] = np.sqrt(particles.px**2 + particles.py**2)
        particles["eta_pt"] = self.calc_eta(particles.pt, particles.pz)
        
        # handle noise
        truth_noise = truth[["hit_id", "particle_id"]][truth.particle_id==0]
        truth_noise["pt"] = 0
        truth = truth[["hit_id", "particle_id"]].merge(
            particles[["particle_id", "pt", "eta_pt", "q", "vx", "vy"]], on="particle_id"
        )
        
        # optionally add noise
        if not self.remove_noise:
            truth = truth.append(truth_noise)

        hits["r"] = np.sqrt(hits.x**2 + hits.y**2)
        hits["phi"] = np.arctan2(hits.y, hits.x)
        hits["eta_rz"] = self.calc_eta(hits.r, hits.z)
        hits["u"] = hits["x"] / (hits["x"] ** 2 + hits["y"] ** 2)
        hits["v"] = hits["y"] / (hits["x"] ** 2 + hits["y"] ** 2)
        hits = hits[
            ["hit_id", "r", "phi", "eta_rz", "x", "y", "z", "u", "v", "volume_id", "layer"]
        ].merge(truth[["hit_id", "particle_id", "pt", "eta_pt"]], on="hit_id")
        return hits
        
    def sector_hits(self, hits, s):
        if (self.n_sectors==1): return hits
        # build sectors in each 2*np.pi/self.n_sectors window
        theta = s*2*np.pi/self.n_sectors
        slope = np.arctan(theta)
        hits['ur'] = hits['u']*np.cos(theta) - hits['v']*np.sin(theta)
        hits['vr'] = hits['u']*np.sin(theta) + hits['v']*np.cos(theta)

        lower_bound = -self.sector_ds * slope * hits.ur - self.sector_di
        upper_bound = self.sector_ds * slope * hits.ur + self.sector_di
        extended_sector = hits[((hits.vr > lower_bound) & 
                                (hits.vr < upper_bound) & 
                                (hits.ur > 0))]
        
        measurements = {}
        if self.measurement_mode:
            sector = hits[((hits.vr > -slope*hits.ur) &
                           (hits.vr < slope*hits.ur) &
                           (hits.ur >= 0))]
            sector_high_pt = sector[sector.pt>self.thld]
            sector_low_pt = sector[sector.pt<=self.thld]
            measurements['sector_size'] = len(sector)
            measurements['extended_sector_size'] = len(extended_sector)
            measurements['sector_size_ratio'] = len(extended_sector)/len(sector)
        return extended_sector, measurements
            
    def to_pyg_data(self, hits):
        data = Data(
            x=hits[self.feature_names].values / self.feature_scale,
            particle_id=hits["particle_id"].values,
            pt=hits["pt"].values,
        )
        return data
    
    def process(self, n=10**6, verbose=False):        
        for i, f in enumerate(self.prefixes):
            if i>=n: break
            print(f"Processing {f}")

            evtid = int(f[-9:])
            hits, particles, truth = load_event(
                f, parts=["hits", "particles", "truth"]
            )

            if self.pixel_only:
                hits = self.restrict_to_pixel(hits)
            n_hits = len(hits)
            hits = self.append_features(hits, particles, truth)
            n_noise = len(hits[hits.particle_id==0])
            n_sector_hits = 0
            for s in range(self.n_sectors):
                name = f"data{evtid}_s{s}.pt"
                if self.exists[evtid] and not self.redo:
                    data = torch.load(join(self.outdir, name))
                    self.data_list.append(data)
                else:
                    sector, measurements = self.sector_hits(hits, s)
                    n_sector_hits += len(sector)
                    sector = self.to_pyg_data(sector)
                    outfile = join(self.outdir, name)
                    if verbose: print(f'...writing {outfile}')
                    torch.save(sector, outfile)
                    self.data_list.append(sector)
            
            self.stats[evtid] = {'n_hits': n_hits,
                                 'n_noise': n_noise,
                                 'n_sector_hits': n_sector_hits}
            print('Output statistics:', self.stats[evtid])
