import gc
import glob
import logging
import os
import random
import warnings
from collections import Iterable
from enum import Enum
from typing import List, Union

import numpy as np
from astropy.visualization import AsinhStretch
from dateutil.parser import parse
from lightning import LightningDataModule
from torch.utils.data import Dataset, DataLoader, RandomSampler
from tqdm import tqdm

from editor import Editor, MapToDataEditor, NanEditor, NormalizeEditor, \
    ExpandDimsEditor, StackEditor, ReshapeEditor, NormalizeExposureEditor, LoadMapEditor, solo_norm, \
    NormalizeRadiusEditor, proba2_norm, AIAPrepEditor, sdo_norms, hri_norm, BrightestPixelPatchEditor, PaddingEditor, \
    RemoveOffLimbEditor


class ITIDataModule(LightningDataModule):

    def __init__(self, A_train_ds, B_train_ds, A_valid_ds, B_valid_ds, iterations_per_epoch=10000, num_workers=4, batch_size=1, **kwargs):
        super().__init__()
        self.A_train_ds = A_train_ds
        self.B_train_ds = B_train_ds
        self.A_valid_ds = A_valid_ds
        self.B_valid_ds = B_valid_ds
        self.num_workers = num_workers
        self.batch_size = batch_size
        self.iterations_per_epoch = iterations_per_epoch

    def train_dataloader(self):
        gen_A = DataLoader(self.A_train_ds, batch_size=self.batch_size, num_workers=self.num_workers,
                           sampler=RandomSampler(self.A_train_ds, replacement=True, num_samples=self.iterations_per_epoch))
        dis_A = DataLoader(self.A_train_ds, batch_size=self.batch_size, num_workers=self.num_workers,
                            sampler=RandomSampler(self.A_train_ds, replacement=True, num_samples=self.iterations_per_epoch))
        gen_B = DataLoader(self.B_train_ds, batch_size=self.batch_size, num_workers=self.num_workers,
                            sampler=RandomSampler(self.B_train_ds, replacement=True, num_samples=self.iterations_per_epoch))
        dis_B = DataLoader(self.B_train_ds, batch_size=self.batch_size, num_workers=self.num_workers,
                            sampler=RandomSampler(self.B_train_ds, replacement=True, num_samples=self.iterations_per_epoch))
        return {"gen_A": gen_A, "dis_A": dis_A, "gen_B": gen_B, "dis_B": dis_B}

    def val_dataloader(self):
        A = DataLoader(self.A_valid_ds, batch_size=self.batch_size, num_workers=self.num_workers)
        B = DataLoader(self.B_valid_ds, batch_size=self.batch_size, num_workers=self.num_workers)
        return [A, B]


class Norm(Enum):
    CONTRAST = 'contrast'
    IMAGE = 'image'
    PEAK = 'adjusted'
    NONE = 'none'

class ArrayDataset(Dataset):

    def __init__(self, data, editors: List[Editor], **kwargs):
        self.data = data
        self.editors = editors

        super().__init__()

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        data, _ = self.getIndex(idx)
        return data

    def sample(self, n_samples):
        it = DataLoader(self, batch_size=1, shuffle=True, num_workers=4).__iter__()
        samples = []
        while len(samples) < n_samples:
            try:
                samples.append(next(it).detach().numpy()[0])
            except Exception as ex:
                logging.error(str(ex))
                continue
        del it
        return np.array(samples)

    def getIndex(self, idx):
        try:
            return self.convertData(self.data[idx])
        except Exception as ex:
            logging.error('Unable to convert %s: %s' % (self.data[idx], ex))
            raise ex

    def getId(self, idx):
        return str(idx)

    def convertData(self, data):
        kwargs = {}
        for editor in self.editors:
            data, kwargs = editor.convert(data, **kwargs)
        return data, kwargs

    def addEditor(self, editor):
        self.editors.append(editor)


class BaseDataset(Dataset):
    def __init__(self, data: Union[str, list], editors: List[Editor], ext: str = None, limit: int = None,
                 months: list = None, date_parser=None, **kwargs):
        if isinstance(data, str):
            pattern = '*' if ext is None else '*' + ext
            data = sorted(glob.glob(os.path.join(data, '**', pattern), recursive=True))
        assert isinstance(data, Iterable), 'Dataset requires list of samples or path of files!'
        if months: #Assuming filename is parsable datetime
            if date_parser is None:
                date_parser = lambda f: parse(os.path.basename(f).split('_')[1])
            data = [d for d in data if date_parser(d).month in months]

        if limit is not None:
            data = random.sample(list(data), limit)
        self.data = data
        self.editors = editors

        super().__init__()

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        data, _ = self.getIndex(idx)
        return data

    def sample(self, n_samples):
        it = DataLoader(self, batch_size=1, shuffle=True, num_workers=4).__iter__()
        samples = []
        while len(samples) < n_samples:
            try:
                samples.append(next(it).detach().numpy()[0])
            except Exception as ex:
                logging.error(str(ex))
                continue
        del it
        return np.array(samples)

    def getIndex(self, idx):
        try:
            return self.convertData(self.data[idx])
        except Exception as ex:
            logging.error('Unable to convert %s: %s' % (self.data[idx], ex))
            raise ex

    def getId(self, idx):
       #return os.path.basename(self.data[idx]).split('.')[1]
        return os.path.basename(self.data[idx])

    def convertData(self, data):
        kwargs = {}
        for editor in self.editors:
            data, kwargs = editor.convert(data, **kwargs)
        return data, kwargs

    def addEditor(self, editor):
        self.editors.append(editor)



class StorageDataset(Dataset):
    def __init__(self, dataset: BaseDataset, store_dir, ext_editors=[]):
        self.dataset = dataset
        self.store_dir = store_dir
        self.ext_editors = ext_editors
        os.makedirs(store_dir, exist_ok=True)

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, idx):
        id = self.dataset.getId(idx)
        store_path = os.path.join(self.store_dir, '%s.npy' % id)
        if os.path.exists(store_path):
            data = np.load(store_path, mmap_mode='r+')
            data = self.convertData(data)
            return data
        data = self.dataset[idx]
        np.save(store_path, data)
        data = self.convertData(data)
        return data

    def convertData(self, data):
        kwargs = {}
        for editor in self.ext_editors:
            data, kwargs = editor.convert(data, **kwargs)
        return data

    def sample(self, n_samples):
        it = DataLoader(self, batch_size=1, shuffle=True, num_workers=4).__iter__()
        samples = []
        while len(samples) < n_samples:
            try:
                samples.append(next(it).detach().numpy())
            except Exception as ex:
                logging.error(str(ex))
                continue
        del it
        return np.concatenate(samples)

    def convert(self, n_worker):
        it = DataLoader(self, batch_size=1, shuffle=False, num_workers=n_worker).__iter__()
        for i in tqdm(range(len(self.dataset))):
            try:
                next(it)
                gc.collect()
            except StopIteration:
                return
            except Exception as ex:
                logging.error('Invalid data: %s' % self.dataset.data[i])
                logging.error(str(ex))
                continue


def get_intersecting_files(path, dirs, months=None, years=None, n_samples=None, ext=None, basenames=None, **kwargs):
    pattern = '*' if ext is None else '*' + ext
    if basenames is None:
        basenames = [[os.path.basename(path) for path in glob.glob(os.path.join(path, str(d), '**', pattern), recursive=True)] for d in dirs]
        basenames = list(set(basenames[0]).intersection(*basenames))
    if months:  # assuming filename is parsable datetime
        basenames = [bn for bn in basenames if parse(bn.split('_')[1]).month in months]
    if years:  # assuming filename is parsable datetime
        basenames = [bn for bn in basenames if parse(bn.split('_')[1]).year in years]
    basenames = sorted(list(basenames))
    if n_samples:
        basenames = basenames[::len(basenames) // n_samples]
    return [[os.path.join(path, str(dir), b) for b in basenames] for dir in dirs]



class StackDataset(BaseDataset):

    def __init__(self, data_sets, limit=None, **kwargs):
        self.data_sets = data_sets

        editors = [StackEditor(data_sets)]
        super().__init__(list(range(len(data_sets[0]))), editors, limit=limit)

    def getId(self, idx):
        return os.path.basename(self.data_sets[0].data[idx]).split('.')[0]


class SDODataset(StackDataset):

    def __init__(self, data, patch_shape=None, resolution=2048, ext='.fits', **kwargs):
        if isinstance(data, list):
            paths = data
        else:
            paths = get_intersecting_files(data, ['171', '193', '211', '304', '6173'], ext=ext, **kwargs)
        data_sets = [AIADataset(paths[0], 171, resolution=resolution, **kwargs),
                     AIADataset(paths[1], 193, resolution=resolution, **kwargs),
                     AIADataset(paths[2], 211, resolution=resolution, **kwargs),
                     AIADataset(paths[3], 304, resolution=resolution, **kwargs),
                     HMIDataset(paths[4], 'mag', resolution=resolution)
                     ]
        super().__init__(data_sets, **kwargs)
        if patch_shape is not None:
            self.addEditor(BrightestPixelPatchEditor(patch_shape))


class AIADataset(BaseDataset):

    def __init__(self, data, wavelength, resolution=2048, ext='.fits', calibration='auto', **kwargs):
        norm = sdo_norms[wavelength]

        editors = [LoadMapEditor(),
                   NormalizeRadiusEditor(resolution),
                   AIAPrepEditor(calibration=calibration),
                   MapToDataEditor(),
                   NormalizeEditor(norm),
                   ReshapeEditor((1, resolution, resolution))]
        super().__init__(data, editors=editors, ext=ext, **kwargs)

class HMIDataset(BaseDataset):

    def __init__(self, data, id, resolution=2048, ext='.fits', **kwargs):
        norm = sdo_norms[id]

        editors = [LoadMapEditor(),
                   NormalizeRadiusEditor(resolution),
                   RemoveOffLimbEditor(),
                   MapToDataEditor(),
                   PaddingEditor((resolution, resolution)),  # fix field-of-view of subframe
                   NanEditor(),
                   NormalizeEditor(norm),
                   ReshapeEditor((1, resolution, resolution))]
        super().__init__(data, editors=editors, ext=ext, **kwargs)


class FSIDataset(BaseDataset):
    def __init__(self, data, wavelength=304, resolution=1024, ext='.fits', **kwargs):
        norm = solo_norm[wavelength]

        editors = [LoadMapEditor(),
                   NormalizeRadiusEditor(resolution),
                   NormalizeExposureEditor(),
                   MapToDataEditor(),
                   NormalizeEditor(norm),
                   ReshapeEditor((1, resolution, resolution))]
        super().__init__(data, editors=editors, ext=ext, **kwargs)


class HRIDataset(BaseDataset):
    def __init__(self, data, ext='.fits', **kwargs):
        norm = hri_norm[174]

        editors = [LoadMapEditor(),
                   NormalizeExposureEditor(),
                   MapToDataEditor(),
                   NormalizeEditor(norm),
                   ExpandDimsEditor()]
        super().__init__(data, editors=editors, ext=ext, **kwargs)



class Proba2Dataset(BaseDataset):
    def __init__(self, data, wavelength=174, resolution=1024, ext='.fits', **kwargs):
        norm = proba2_norm[wavelength]

        editors = [LoadMapEditor(),
                   NormalizeRadiusEditor(resolution),
                   NormalizeExposureEditor(),
                   MapToDataEditor(),
                   NormalizeEditor(norm),
                   ReshapeEditor((1, resolution, resolution))]
        super().__init__(data, editors=editors, ext=ext, **kwargs)


