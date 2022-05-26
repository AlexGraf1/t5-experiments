import argparse
import json
import logging
import os
from pathlib import Path

from megatron.data.dataset_utils import get_indexed_dataset_

import horovod.torch as hvd
from dotenv import load_dotenv
import torch
from torch.utils.data import DataLoader, DistributedSampler, Dataset
import numpy as np
import pandas as pd
from sklearn.metrics import f1_score, precision_score, recall_score, matthews_corrcoef

from trainer import Trainer

load_dotenv()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

# if CUDA_VISIBLE_DEVICES is not set make all gpus visible
if os.environ.get('CUDA_VISIBLE_DEVICES', None) is None:
    os.environ['CUDA_VISIBLE_DEVICES'] = ','.join([str(i) for i in range(torch.cuda.device_count())])

logger.info(f"CUDA_VISIBLE_DEVICES: {os.environ['CUDA_VISIBLE_DEVICES']}")
# first call to torch.cuda.device_count() sets visible gpus, following calls will not change the result
logger.info(f"CUDA DEVICE COUNT: {torch.cuda.device_count()}")

hvd.init()

import transformers  # noqa: E402
from transformers import AutoConfig  # noqa: E402
from transformers import AutoTokenizer  # noqa: E402

from utils import collect_run_configuration, get_cls_by_name, get_optimizer  # noqa: E402
import optimizers  # noqa: E402

# limit # of CPU threads to be used per pytorch worker, otherwise it might use all cpus and throttle gpus
# > 2 fails cause of https://github.com/pytorch/pytorch/issues/56615
# need to upgrade to torch>1.8.1
torch.set_num_threads(4)
# all gpus set with CUDA_VISIBLE_DEVICES are visible to process, indexing from 0 to ...
torch.cuda.set_device(hvd.local_rank())

parser = argparse.ArgumentParser()
parser.add_argument('--model_path', type=str, default=None, help='path where to save model (default: None)')
parser.add_argument('--data_path', type=str, help='path the training data, could be a folder')
parser.add_argument('--valid_data_path', type=str, help='path the valid data, could be a folder')
parser.add_argument('--test_data_path', type=str, help='path the test data, could be a folder')
parser.add_argument('--log_interval', type=int, default=10,
                    help='how many batches to wait for logging training status')
parser.add_argument('--valid_interval', type=int, default=None,
                    help='how many batches to wait for logging training status')
parser.add_argument('--validate_only', action='store_true', default=False,
                    help='Skip training and run only validation. (default: False)')
parser.add_argument('--save_interval', type=int, default=5000, help='save model every steps')
parser.add_argument('--save_best', action='store_true', default=False,
                    help='Save best checkpoint if validation set is provided.')
parser.add_argument('--working_dir', type=str, default='.',
                    help='working dir, should be a dir with t5-experiments repo (default: .)')
parser.add_argument('--seed', type=int, default=42, help='random seed')

# bert data args
parser.add_argument('--input_seq_len', type=int, default=128, help='input sequnce length (default: 128).')
parser.add_argument('--data_n_workers', type=int, default=2, help='number of dataloader workers (default: 2)')

# model args
parser.add_argument('--model_cfg', type=str, help='path to model configuration file (default: None)')
parser.add_argument('--model_cls', type=str, default='transformers:BertForPreTraining',
                    help='model class name to use (default: transformers:BertForPreTraining)')
parser.add_argument('--init_checkpoint', type=str, help='path to init checkpoint to load a model from (default: None).')
parser.add_argument('--skip_used_data', action='store_true', default=False,
                    help='skip batches that were already seen by init_checkpoint (default: False)')

# tokenizer
# todo: add wordpiece tokenizers support?
parser.add_argument('--tokenizer', type=str, default=None, help='path or name of pre-trained HF Tokenizer')

# training args
parser.add_argument('--lr', type=float, default=None, help='learning rate (default: None)')
parser.add_argument('--batch_size', type=int, default=10, help='input batch size for training (default: 10)')
parser.add_argument('--iters', type=int, default=100,
                    help='number of training steps (i.e., gradient updates) (default: 100).')
parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                    help='number of batches to accumulate gradients for each worker; it multiplies total batch size.')
parser.add_argument('--fp16-allreduce', action='store_true', default=False,
                    help='use fp16 compression during allreduce')
parser.add_argument('--fp16', action='store_true', default=False, help='use torch.amp for fp16 training')
parser.add_argument('--apex_opt_lvl', type=str, default='O1', help='apex opt level, O1, O2. (default: O1)')
parser.add_argument('--min_loss_scale', type=float, default=None, help='apex min_loss_scale. (default: None)')
parser.add_argument('--clip_grad_norm', type=float, default=None,
                    help='torch.nn.utils.clip_grad_norm_ max_norm parameter. (default: None)')
parser.add_argument('--clip_grad_value', type=float, default=None,
                    help='torch.nn.utils.clip_grad_value_ clip_value parameter. (default: None)')

# optimizer args
parser.add_argument('--optimizer', type=str, default='AdamW', help='optimizer name: AdamW, Adafactor. (default: AdamW)')
parser.add_argument('--weight_decay', type=float, default=0.0, help='optimizer weight decay (default: 0.0)')
parser.add_argument('--scale_parameter', action='store_true', default=False,
                    help='Adafactor scale_parameter (default: False)')
parser.add_argument('--relative_step', action='store_true', default=False,
                    help='Adafactor relative_step (default: False)')
parser.add_argument('--warmup_init', action='store_true', default=False,
                    help='Adafactor warmup_init (default: False)')
parser.add_argument('--reset_optimizer', action='store_true', default=False,
                    help='Do not load optimizer from checkpoint and setup a new one. It might help for continuing '
                    'training of models trained with fp16 O2. Otherwise spikes in loss might happen. (default: False)')

# scheduler args
parser.add_argument('--lr_scheduler', type=str, default=None,
                    help='scheduler name from transformers.optimization: linear, cosine, cosine_with_restarts, '
                    'polynomial, constant, constant_with_warmup (default: None)')
parser.add_argument('--num_warmup_steps', type=int, default=None,
                    help='number of warming steps to get to lr (default: None)')
parser.add_argument('--num_training_steps', type=int, default=None,
                    help='number of training steps, if not set iters will be used (default: None)')
parser.add_argument('--reset_lr', action='store_true', default=False,
                    help='Do not load lr_scheduler from checkpoint and setup new (default: False)')
parser.add_argument('--reset_iteration', action='store_true', default=False,
                    help='Do not load iteration number from checkpoint and set it to 0 (default: False)')

# ReduceLROnPlateau args
parser.add_argument('--use_lr_drop', action='store_true', default=False,
                    help='Enable ReduceLROnPlateau scheduler in addition to --lr_scheduler (default: False)')
parser.add_argument('--lr_drop_factor', type=float, default=0.1,
                    help='torch.optim.lr_scheduler.ReduceLROnPlateau drop parameter. (default: 0.1)')
parser.add_argument('--lr_drop_patience', type=int, default=10,
                    help='torch.optim.lr_scheduler.ReduceLROnPlateau patience parameter. (default: 10)')
parser.add_argument('--lr_drop_threshold', type=float, default=1e-04,
                    help='torch.optim.lr_scheduler.ReduceLROnPlateau threshold parameter. (default: 1e-04)')
parser.add_argument('--lr_drop_threshold_mode', type=str, default='rel',
                    help='torch.optim.lr_scheduler.ReduceLROnPlateau threshold_mode parameter. (default: rel)')
parser.add_argument('--lr_drop_cooldown', type=int, default=0,
                    help='torch.optim.lr_scheduler.ReduceLROnPlateau cooldown parameter. (default: 0)')
parser.add_argument('--lr_drop_min_lr', type=float, default=0.0,
                    help='torch.optim.lr_scheduler.ReduceLROnPlateau min_lr parameter. (default: 0.0)')
parser.add_argument('--lr_drop_eps', type=float, default=1e-08,
                    help='torch.optim.lr_scheduler.ReduceLROnPlateau threshold_mode parameter. (default: 1e-08)')

# metrics args
parser.add_argument('--optimize_metric', type=str, default='loss',
                    help='metric name to optimize, choose the best model & drop lr on patience (default: loss)')
parser.add_argument('--optimize_mode', type=str, default='min',
                    help='metric should be minimized (min) or maximized (max) (default: min)')


class EPDnewPromoterDataset(Dataset):
    def __init__(self, datafiles, tokenizer, x_field='x', label_field='label', max_seq_len=512, pad_to_max=True):
        if isinstance(datafiles, str):
            # convert str path to folder to Path
            datafiles = Path(datafiles)
        if isinstance(datafiles, Path) and datafiles.is_dir():
            # get all files from folder
            datafiles = list(datafiles.iterdir())
        self.data = pd.DataFrame()
        for f in datafiles:
            self.data = pd.concat([self.data, pd.read_csv(f)])
        self.data = self.data.reset_index()
        self.x_field = x_field
        self.label_field = label_field
        self.tokenizer = tokenizer
        self.max_seq_len = max_seq_len
        self.pad_to_max = pad_to_max

    @staticmethod
    def get_features(x, tokenizer, max_seq_len=512, pad_to_max=True):
        tokens = [tokenizer.cls_token] + tokenizer.tokenize(x)[:max_seq_len-2] + [tokenizer.sep_token]
        input_ids = tokenizer.convert_tokens_to_ids(tokens)
        seq_len = len(tokens)
        token_type_ids = [0] * seq_len
        attention_mask = [1] * seq_len
        if pad_to_max:
            input_ids += [tokenizer.pad_token_id] * max(max_seq_len - seq_len, 0)
            token_type_ids += [0] * max(max_seq_len - seq_len, 0)
            attention_mask += [0] * max(max_seq_len - seq_len, 0)
        return {'input_ids': np.array(input_ids),
                'token_type_ids': np.array(token_type_ids),
                'attention_mask': np.array(attention_mask)}

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        x = self.data[self.x_field][idx]
        features = EPDnewPromoterDataset.get_features(x, self.tokenizer, self.max_seq_len, self.pad_to_max)
        label = {'labels': self.data[self.label_field][idx]}
        return {**features, **label}


if __name__ == '__main__':
    args = parser.parse_args()
    # set current working dir
    args.working_dir = str(Path(args.working_dir).expanduser().absolute())
    os.chdir(args.working_dir)
    if hvd.rank() == 0:
        logger.info(f'hvd size: {hvd.size()}')
        logger.info(f'FP16: {args.fp16}')

    if hvd.rank() == 0 and args.model_path is None:
        logger.warning('model_path is not set: config, logs and checkpoints will not be saved.')

    # create model path and save configuration
    if hvd.rank() == 0 and args.model_path is not None:
        model_path = Path(args.model_path)
        if not model_path.exists():
            Path(model_path).mkdir(parents=True)
        args_dict = collect_run_configuration(args)
        # todo: if model path exists and there is config file, write new config file aside
        json.dump(args_dict, open(model_path/'config.json', 'w'), indent=4)

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
    # get train dataset
    if hvd.rank() == 0:
        logger.info(f'preparing training data from: {args.data_path}')
    data_path = Path(args.data_path).expanduser().absolute()
    train_dataset = EPDnewPromoterDataset(data_path, tokenizer, x_field='sequence', label_field='promoter_presence',
                                          max_seq_len=args.input_seq_len, pad_to_max=True)

    # shuffle train data each epoch (one loop over train_dataset)
    train_sampler = DistributedSampler(train_dataset, rank=hvd.rank(), num_replicas=hvd.size(), shuffle=True,
                                       drop_last=False, seed=args.seed)

    per_worker_batch_size = args.batch_size * args.gradient_accumulation_steps
    global_batch_size = per_worker_batch_size * hvd.size()
    kwargs = {'pin_memory': True, 'num_workers': args.data_n_workers}

    train_dataloader = DataLoader(train_dataset, batch_size=per_worker_batch_size, sampler=train_sampler, **kwargs)
    # get validation dataset
    if args.valid_data_path:
        if hvd.rank() == 0:
            logger.info(f'preparing validation data from: {args.valid_data_path}')
        valid_data_path = Path(args.valid_data_path).expanduser().absolute()
        valid_dataset = EPDnewPromoterDataset(valid_data_path, tokenizer, x_field='sequence',
                                              label_field='promoter_presence', max_seq_len=args.input_seq_len,
                                              pad_to_max=True)
        valid_sampler = DistributedSampler(valid_dataset, rank=hvd.rank(), num_replicas=hvd.size(), shuffle=False)
        valid_dataloader = DataLoader(valid_dataset, batch_size=per_worker_batch_size, sampler=valid_sampler, **kwargs)
        if args.valid_interval is None:
            args.valid_interval = args.log_interval
    else:
        valid_dataloader = None
        if hvd.rank() == 0:
            logger.info('No validation data is used.')
    # get test dataset
    if args.test_data_path:
        if hvd.rank() == 0:
            logger.info(f'preparing test data from: {args.test_data_path}')
        test_data_path = Path(args.test_data_path).expanduser().absolute()
        test_dataset = EPDnewPromoterDataset(test_data_path, tokenizer, x_field='sequence',
                                             label_field='promoter_presence', max_seq_len=args.input_seq_len,
                                             pad_to_max=True)
        test_sampler = DistributedSampler(test_dataset, rank=hvd.rank(), num_replicas=hvd.size(), shuffle=False)
        test_dataloader = DataLoader(test_dataset, batch_size=per_worker_batch_size, sampler=test_sampler, **kwargs)

    # define model
    model_cfg = AutoConfig.from_pretrained(args.model_cfg)
    # todo: get model class from model_cfg?
    model_cls = get_cls_by_name(args.model_cls)
    if hvd.rank() == 0:
        logger.info(f'Using model class: {model_cls}')
    model = model_cls(config=model_cfg)

    # define optimizer
    # todo: move to trainer?
    optimizer_cls = get_optimizer(args.optimizer)
    if optimizer_cls is None:
        raise RuntimeError(f'{args.optimizer} was not found in optimizers, torch.optim, transformers.optimization')

    if hvd.rank() == 0:
        logger.info(f'Using optimizer class: {optimizer_cls}')

    # todo: group optimizer params
    if optimizer_cls in [transformers.optimization.Adafactor, optimizers.Adafactor]:
        # https://github.com/huggingface/transformers/pull/9751/files -> transformers 4.3.0
        optimizer = optimizer_cls(model.parameters(), lr=args.lr,
                                  scale_parameter=args.scale_parameter,
                                  relative_step=args.relative_step,
                                  warmup_init=args.warmup_init,
                                  weight_decay=args.weight_decay)
    else:
        optimizer = optimizer_cls(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    def keep_for_metrics_fn(batch, output):
        # select data from batch and model output that would be used to compute metrics
        data = {}
        data['labels'] = batch['labels']
        data['predictions'] = torch.argmax(output['logits'].detach(), dim=-1)
        return data

    def metrics_fn(data):
        # compute metrics based on stored labels, predictions, ...
        metrics = {}
        y, p = data['labels'], data['predictions']
        # accuracy
        metrics['accuracy'] = (p == y).sum() / len(y)
        # f1, precision, recall, mcc
        metrics['f1'] = f1_score(y, p)
        metrics['precision'] = precision_score(y, p)
        metrics['recall'] = recall_score(y, p)
        metrics['mcc'] = matthews_corrcoef(y, p)
        return metrics

    trainer = Trainer(args, model, optimizer, train_dataloader, valid_dataloader, train_sampler,
                      keep_for_metrics_fn=keep_for_metrics_fn, metrics_fn=metrics_fn)

    if not args.validate_only:
        # train loop
        trainer.train()
        # make sure all workers are done
        hvd.barrier()
        # run validation after training
        if args.save_best:
            best_model_path = str(Path(args.model_path) / 'model_best.pth')
            if hvd.rank() == 0:
                logger.info(f'Loading best saved model from {best_model_path}')
            trainer.load(best_model_path)
        if args.valid_data_path:
            if hvd.rank() == 0:
                logger.info('Runnning validation on valid data:')
            trainer.validate(valid_dataloader, write_tb=False)
        if args.test_data_path:
            if hvd.rank() == 0:
                logger.info('Runnning validation on test data:')
            trainer.validate(test_dataloader, split='test', write_tb=True)
    else:
        # run validation, do not write to tensorboard
        if hvd.rank() == 0:
            logger.info('Running validation on train set:')
        trainer.validate(train_dataloader, write_tb=False)
        if args.valid_data_path:
            if hvd.rank() == 0:
                logger.info('Running validation on valid data:')
            trainer.validate(valid_dataloader, write_tb=False)
        if args.test_data_path:
            if hvd.rank() == 0:
                logger.info('Running validation on test data:')
            trainer.validate(test_dataloader, split='test', write_tb=False)