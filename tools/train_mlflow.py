# Copyright 2021 RangiLyu.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import torch
import argparse
import numpy as np
import warnings
import pytorch_lightning as pl
from pytorch_lightning.callbacks import ProgressBar
from pytorch_lightning.loggers import MLFlowLogger
from mlflow.tracking import MlflowClient
from mlflow.tracking.context import registry as context_registry
from mlflow.utils.mlflow_tags import MLFLOW_RUN_NAME

from nanodet.util import mkdir, Logger, cfg, load_config, convert_old_model
from nanodet.data.collate import collate_function
from nanodet.data.dataset import build_dataset
from nanodet.trainer.task import TrainingTask
from nanodet.evaluator import build_evaluator
from nanodet.util import load_model_weight
    
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('config', help='train config file path')
    parser.add_argument('--local_rank', default=-1, type=int,
                        help='node rank for distributed training')
    parser.add_argument('--seed', type=int, default=None,
                        help='random seed')
    parser.add_argument('--use_mlflow', action='store_true')
    parser.add_argument('--mlflow_uri', type=str, default='file:./ml-runs', help='mlflow tracking_uri')
    parser.add_argument('--mlflow_run', type=str, default='default', help='mlflow run name')
    parser.add_argument('--mlflow_experiment', type=str, default='default', help='mlflow experiment name')
    args = parser.parse_args()
    return args


def main(args):
    load_config(cfg, args.config)
    if cfg.model.arch.head.num_classes != len(cfg.class_names):
        raise ValueError('cfg.model.arch.head.num_classes must equal len(cfg.class_names), '
                         'but got {} and {}'.format(cfg.model.arch.head.num_classes, len(cfg.class_names)))
    local_rank = int(args.local_rank)
    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = True
    mkdir(local_rank, cfg.save_dir)
    logger = Logger(local_rank, cfg.save_dir)

    if args.seed is not None:
        logger.log('Set random seed to {}'.format(args.seed))
        pl.seed_everything(args.seed)

    logger.log('Setting up data...')
    train_dataset = build_dataset(cfg.data.train, 'train')
    val_dataset = build_dataset(cfg.data.val, 'test')

    evaluator = build_evaluator(cfg, val_dataset)

    train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size=cfg.device.batchsize_per_gpu,
                                                   shuffle=True, num_workers=cfg.device.workers_per_gpu,
                                                   pin_memory=True, collate_fn=collate_function, drop_last=True)
    val_dataloader = torch.utils.data.DataLoader(val_dataset, batch_size=cfg.device.batchsize_per_gpu,
                                                 shuffle=False, num_workers=cfg.device.workers_per_gpu,
                                                 pin_memory=True, collate_fn=collate_function, drop_last=False)

    logger.log('Creating model...')
    
    if args.use_mlflow:
        log_style='Lightning'
    else:
        log_style='NanoDet'        
    task = TrainingTask(cfg, evaluator, log_style=log_style)
    
    if 'load_model' in cfg.schedule:
        ckpt = torch.load(cfg.schedule.load_model)
        if 'pytorch-lightning_version' not in ckpt:
            warnings.warn('Warning! Old .pth checkpoint is deprecated. '
                          'Convert the checkpoint with tools/convert_old_checkpoint.py ')
            ckpt = convert_old_model(ckpt)

        load_model_weight(task.model, ckpt, logger)
        logger.log('Loaded model weight from {}'.format(cfg.schedule.load_model))

    model_resume_path = os.path.join(cfg.save_dir, 'model_last.ckpt') if 'resume' in cfg.schedule else None



    if args.use_mlflow:
        user_specified_tags = {}  # your own tags here
        user_specified_tags[MLFLOW_RUN_NAME] = args.mlflow_run
        tags = context_registry.resolve_tags(user_specified_tags)
        mlf_logger = MLFlowLogger(
            experiment_name=args.mlflow_experiment,
            tags=tags,
            tracking_uri=args.mlflow_uri
        )
        used_logger = mlf_logger
        bar_refresh=1
    else:
        used_logger = True    
        bar_refresh=0

    trainer = pl.Trainer(default_root_dir=cfg.save_dir,
                         max_epochs=cfg.schedule.total_epochs,
                         gpus=cfg.device.gpu_ids,
                         check_val_every_n_epoch=cfg.schedule.val_intervals,
                         accelerator='ddp',
                         log_every_n_steps=cfg.log.interval,
                         num_sanity_val_steps=0,
                         resume_from_checkpoint=model_resume_path,
                         callbacks=[ProgressBar(refresh_rate=bar_refresh)],  # disable tqdm bar with rate=0
                         benchmark=True,
                         logger=used_logger,
                         )
   
    # Train the model
    trainer.fit(task, train_dataloader, val_dataloader)


if __name__ == '__main__':
    args = parse_args()
    main(args)
