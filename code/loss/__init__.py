import os
from importlib import import_module

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

class Loss(nn.modules.loss._Loss):
    def __init__(self, args, ckp):
        super(Loss, self).__init__()
        print('Preparing loss function:')

        self.n_GPUs = args.n_GPUs
        self.loss = []
        self.loss_module = nn.ModuleList()
        self.intensity_loss = args.intensity_loss
        self.normalize = args.normalized_loss
        self.rgb_range = float(args.rgb_range)

        if args.intensity_loss:
            self.intensity_conv = torch.zeros((1,3,1,1),requires_grad=False)
            if args.rgb_range == 255: 
                self.intensity_conv[0, 0, 0, 0] = 0.299
                self.intensity_conv[0, 1, 0, 0] = 0.587
                self.intensity_conv[0, 2, 0, 0] = 0.114
            elif args.rgb_range == 1: 
                raise NotImplementedError()
            

        for loss in args.loss.split('+'):
            weight, loss_type = loss.split('*')
            if loss_type == 'MSE':
                loss_function = nn.MSELoss()
            elif loss_type == 'L1':
                loss_function = nn.L1Loss()
            elif loss_type == 'Charbonnier':
                module = import_module('loss.charbonnier')
                loss_function = getattr(module, 'Charbonnier')()
            elif loss_type == 'GradL2':
                module = import_module('loss.gradl2')
                loss_function = getattr(module, 'GradL2')(args.n_channel_out,
                                                        not args.cpu)
            elif loss_type == 'SSIM': 
                module = import_module('loss.ssim')
                loss_function = getattr(module, 'SSIM')(args.patch_size, 
                                                        args.batch_size,
                                                        args.n_channel_out,
                                                        not args.cpu)
            elif loss_type == 'MSSSIM': 
                module = import_module('loss.msssim')
                loss_function = getattr(module, 'MSSSIM')(args.patch_size, 
                                                        args.batch_size,
                                                        args.n_channel_out,
                                                        not args.cpu)
            elif loss_type == 'WeightedMSE': 
                module = import_module('loss.weightedmse')
                loss_function = getattr(module, 'WeightedMSE')()
            elif loss_type.find('VGG') >= 0:
                module = import_module('loss.vgg')
                loss_function = getattr(module, 'VGG')(
                    loss_type[3:],
                    rgb_range=args.rgb_range
                )
            elif loss_type.find('GAN') >= 0:
                module = import_module('loss.adversarial')
                loss_function = getattr(module, 'Adversarial')(
                    args,
                    loss_type
                )
           
            self.loss.append({
                'type': loss_type,
                'weight': float(weight),
                'function': loss_function}
            )
            if loss_type.find('GAN') >= 0:
                self.loss.append({'type': 'DIS', 'weight': 1, 'function': None})

        if len(self.loss) > 1:
            self.loss.append({'type': 'Total', 'weight': 0, 'function': None})

        for l in self.loss:
            if l['function'] is not None:
                print('{:.3f} * {}'.format(l['weight'], l['type']))
                self.loss_module.append(l['function'])

        self.log = torch.Tensor()

        device = torch.device('cpu' if args.cpu else 'cuda')
        self.loss_module.to(device)
        #if not args.cpu: self.intensity_conv = self.intensity_conv.cuda()

        if args.precision == 'half': self.loss_module.half()
        if not args.cpu and args.n_GPUs > 1:
            self.loss_module = nn.DataParallel(
                self.loss_module, range(args.n_GPUs)
            )

        if args.load != '.': self.load(ckp.dir, cpu=args.cpu)

    def forward(self, sr, hr):
        losses = []

        if self.intensity_loss:
            sr = nn.functional.conv2d(sr, self.intensity_conv)
            hr = nn.functional.conv2d(hr, self.intensity_conv)

        if self.normalize: 
            sr = sr / self.rgb_range
            hr = hr / self.rgb_range
            
        for i, l in enumerate(self.loss):
            if l['function'] is not None:
                loss = l['function'](sr, hr)
                effective_loss = l['weight'] * loss
                losses.append(effective_loss)
                self.log[-1, i] += effective_loss.item()
            elif l['type'] == 'DIS':
                self.log[-1, i] += self.loss[i - 1]['function'].loss

        loss_sum = sum(losses)
        if len(self.loss) > 1:
            self.log[-1, -1] += loss_sum.item()

        return loss_sum

    def step(self):
        for l in self.get_loss_module():
            if hasattr(l, 'scheduler'):
                l.scheduler.step()

    def start_log(self):
        self.log = torch.cat((self.log, torch.zeros(1, len(self.loss))))

    def end_log(self, n_batches):
        self.log[-1].div_(n_batches)

    def display_loss(self, batch):
        n_samples = batch + 1
        log = []
        for l, c in zip(self.loss, self.log[-1]):
            log.append('[{}: {:.4f}]'.format(l['type'], c / n_samples))

        return ''.join(log)

    def plot_loss(self, apath, epoch):
        axis = np.linspace(1, epoch, epoch)
        for i, l in enumerate(self.loss):
            label = '{} Loss'.format(l['type'])
            fig = plt.figure()
            plt.title(label)
            plt.plot(axis, self.log[:, i].numpy(), label=label)
            plt.legend()
            plt.xlabel('Epochs')
            plt.ylabel('Loss')
            plt.grid(True)
            plt.savefig('{}/loss_{}.pdf'.format(apath, l['type']))
            plt.close(fig)

    def get_loss_module(self):
        if self.n_GPUs == 1:
            return self.loss_module
        else:
            return self.loss_module.module

    def save(self, apath):
        torch.save(self.state_dict(), os.path.join(apath, 'loss.pt'))
        torch.save(self.log, os.path.join(apath, 'loss_log.pt'))

    def load(self, apath, cpu=False):
        if cpu:
            kwargs = {'map_location': lambda storage, loc: storage}
        else:
            kwargs = {}

        self.load_state_dict(torch.load(
            os.path.join(apath, 'loss.pt'),
            **kwargs
        ))
        self.log = torch.load(os.path.join(apath, 'loss_log.pt'))
        for l in self.loss_module:
            if hasattr(l, 'scheduler'):
                for _ in range(len(self.log)): l.scheduler.step()

