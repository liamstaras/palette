import torch
import random
import tqdm
from data import Saver
from support import NoiseSchedule, TorchMetric
from torch.utils.data import DataLoader
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
import numpy as np
from pprint import pformat
import os

# define a placeholder class for diffusion models, demonstrating the necessity of a refinement_step method
class DiffusionModel(torch.nn.Module):
    def refinement_step(self, predicted_gt_image_t, cond_image, alpha_t, gamma_t):
        raise AttributeError('Must define a refinement step!')

# a class containing the main algorithms for any diffusion model
class DiffusionFramework:
    def __init__(
        self,
        device: str,
        model: DiffusionModel,
        optimizer: torch.optim.Optimizer,
        scheduler: torch.optim.lr_scheduler._LRScheduler,
        training_dataloader: DataLoader,
        training_noise_schedule: NoiseSchedule,
        training_loss_function: torch.nn.modules.loss._Loss,
        evaluation_dataloader: DataLoader,
        inference_noise_schedule: NoiseSchedule,
        evaluation_metrics: list[TorchMetric],
        base_path: str,
        summary_writer = None,
        save_functions: list[Saver] = [],
        visual_function: Callable[[torch.Tensor], np.ndarray] = lambda tensor: tensor.cpu().numpy()
    ):
        ## properties specified as arguments
        self.device = device
        self.model = model.to(device)
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.training_dataloader = training_dataloader
        self.training_noise_schedule = training_noise_schedule
        self.training_loss_function = training_loss_function
        self.evaluation_dataloader = evaluation_dataloader
        self.inference_noise_schedule = inference_noise_schedule
        self.evaluation_metrics = evaluation_metrics
        self.base_path = base_path
        self.summary_writer = summary_writer
        self.save_functions = save_functions
        self.visual_function = visual_function

        ## initialize path and subdirectories
        self.log_path = os.path.join(self.base_path, 'logfile.log')
        self.image_base_path = os.path.join(self.base_path, 'output')
        self.model_base_path = os.path.join(self.base_path, 'models')
        self.loss_function_name = self.training_loss_function.__class__.__name__
        os.makedirs(self.base_path, exist_ok=True)
        os.makedirs(self.image_base_path, exist_ok=True)
        os.makedirs(self.model_base_path, exist_ok=True)


    def train_single_epoch(self, epoch_number, log_every):
        # place the model into training mode
        self.model.train()
        # zero loss counters
        running_loss = 0.
        current_loss = 0.
        # loop over the training data, showing tqdm progress bar and tracking the index
        # use tqdm to show a progress bar, to which we can affix the current loss rate
        pbar = tqdm.tqdm(self.training_dataloader, postfix='current learning rate: --------, current loss: ------')
        # using zero indexing is annoying for mean calc, so start from 1
        for i, data in enumerate(pbar, start=1):
            # extract the images from the loaded data
            gt_image, cond_image, mask = data
            # add the loss to the cumulative total
            current_loss = self.train_one_batch(gt_image.to(self.device), cond_image.to(self.device), mask.to(self.device))
            # display the current loss
            pbar.set_postfix_str('current learning rate: {:.2e}, current loss: {:.4f}'.format(float(self.optimizer.param_groups[-1]['lr']), float(current_loss)))
            running_loss += current_loss
            # check if we are at a logging iteration
            if i % log_every == 0:
                # find the mean loss over the past logging group
                mean_loss = running_loss/log_every
                # write to the log
                self.write_log_line(
                    'Epoch {: >3d}, iteration {: >6d}. Current learning rate is {:.2e}. Mean loss: {:.5f}.'.format(
                        epoch_number,
                        i,
                        self.optimizer.param_groups[-1]['lr'],
                        mean_loss
                    )
                )
                # determine the global index for logging purposes
                global_index = (epoch_number-1)*len(self.training_dataloader) + i
                # write to tensorboard (if initialized)
                self.log_scalar('train/'+self.loss_function_name, mean_loss, global_index)
                # zero the running counter
                running_loss = 0.
        # return the most recent loss
        return mean_loss


    def train_one_batch(self, gt_image: torch.Tensor, cond_image: torch.Tensor, mask: torch.BoolTensor):
        # reset optimizer gradients
        self.optimizer.zero_grad()
        # randomly pick t, and sample the corresponding gamma
        t = random.randrange(len(self.training_noise_schedule))
        gamma = torch.tensor(self.training_noise_schedule.gammas[t], device=self.device)
        # generate N(0,1) noise field
        noise_field = torch.randn_like(gt_image, device=self.device)
        # add noise to the gt_image at an appropriate level
        noisy_image = torch.sqrt(gamma)*gt_image + torch.sqrt(1-gamma)*noise_field
        # return predict the noise field using the nn
        predicted_noise_field = self.model(cond_image, noisy_image, gamma)
        # calculate loss
        loss = self.training_loss_function(noise_field, predicted_noise_field)
        loss.backward()
        # adjust learning weights
        self.optimizer.step()
        return loss

    def evaluate_single_epoch(self, epoch_number):
        # place the model into evaluation mode
        self.model.eval()
        # make ordered dict to store lists of metric results
        all_metric_results = OrderedDict(
            (metric.name, []) for metric in self.evaluation_metrics
        )
        # loop over the evaluation data, showing tqdm progress bar and tracking the index
        for i, data in enumerate(tqdm.tqdm(self.evaluation_dataloader)):
            gt_image, cond_image, mask = data
            # get the current metric results
            predicted_gt_image, metric_results = self.evaluate_one_batch(gt_image.to(self.device), cond_image.to(self.device), mask.to(self.device))
            # loop over all metrics, and add the result for each image in the batch to the list for this epoch
            for key in metric_results:
                all_metric_results[key].extend(metric_results[key])
        # create a new OrderedDict to store the mean metric results, by dividing through by the length of the dataloader
        mean_metric_results = OrderedDict(
            # use nanmean to avoid polluting the mean with any stray NaNs
            (key, np.nanmean(all_metric_results[key])) for key in metric_results
        )
        # log the FINAL visual from each batch
        self.log_visuals('Evaluation', epoch_number, cond_image[-1], predicted_gt_image[-1], gt_image[-1], mask[-1])
        return mean_metric_results

    def evaluate_one_batch(self, gt_image: torch.Tensor, cond_image: torch.Tensor, mask: torch.BoolTensor):
        # carry out inference to predict the ground truth
        predicted_gt_image = self.infer_one_batch(cond_image, mask)
        # run evaluation metrics on the image
        metric_results = OrderedDict(
            (metric.name, metric(output=predicted_gt_image, target=gt_image)) for metric in self.evaluation_metrics
        )
        return predicted_gt_image, metric_results


    def infer_one_batch(self, cond_image: torch.Tensor, mask: torch.BoolTensor):
        # start with a noise field, inserting the cond_image where the mask is present
        predicted_gt_image = torch.randn_like(cond_image)*mask + cond_image*(1-mask)
        # iteratively apply the refinement step to denoise and renoise the image
        # note: t runs from T to 1
        for i in tqdm.tqdm(range(len(self.inference_noise_schedule))):
            t = len(self.inference_noise_schedule) - (i+1)
            # actually predict an image, and apply the mask
            predicted_gt_image = self.model.refinement_step(
                predicted_gt_image_t=predicted_gt_image,
                cond_image=cond_image,
                alpha_t=torch.tensor(self.inference_noise_schedule.alphas[t], device=self.device),
                gamma_t=torch.tensor(self.inference_noise_schedule.gammas[t], device=self.device)
            )*mask + cond_image*(1-mask)
        return predicted_gt_image
    
    def save(self, epoch_number, best=False):
        # add "_BEST" if this was the best epoch so far
        _best = '_BEST' if best else ''
        # generate output name
        name = 'model_{}{}'.format(epoch_number, _best)
        # save model to file
        torch.save(self.model.state_dict(), os.path.join(self.model_base_path, name))
    
    def write_log_line(self, line: str, date_time=True, also_print=False):
        if date_time:
            line = datetime.now().strftime('%Y%m%d_%H%M%S: ') + line
        if also_print: print(line)
        with open(self.log_path, 'a') as logfile:
            logfile.write(line+'\n')
    
    def log_scalar(self, series_name, y_value, x_value):
        # if a tensorboard instance has been passed, use it to log the scalar
        if self.summary_writer is not None:
            self.summary_writer.add_scalar(series_name, y_value, x_value)
    
    def log_visuals(self, series_name, index, cond_image, predicted_gt_image, gt_image, mask):
        cond_image_out = cond_image.squeeze()
        predicted_gt_image_out = predicted_gt_image.squeeze()
        gt_image_out = gt_image.squeeze()
        if self.summary_writer is not None:
            self.summary_writer.add_image(series_name+'/Conditioned', self.visual_function(cond_image_out), index)
            self.summary_writer.add_image(series_name+'/Predicted', self.visual_function(predicted_gt_image_out), index)
            self.summary_writer.add_image(series_name+'/Ground Truth', self.visual_function(gt_image_out), index)
        for writer in self.save_functions:
            name = '{}_{}_{:d}'.format(series_name, '{}', index)
            writer(cond_image_out, self.image_base_path, name=name.format('Cond'))
            writer(predicted_gt_image_out, self.image_base_path, name=name.format('Pred'))
            writer(gt_image_out, self.image_base_path, name=name.format('GT'))

    def main_training_loop(self, log_every=100, eval_every=1, save_every=1):
        epoch_number = 1
        best_rms_metrics = None
        while True:
            self.write_log_line('Begin epoch '+str(epoch_number), also_print=True)
            self.write_log_line('Beginning training...', also_print=True)
            self.train_single_epoch(epoch_number, log_every)
            saved = False
            if epoch_number % eval_every == 0:
                self.write_log_line('This is an evaluation epoch. Beginning evalution...', also_print=True)
                mean_metric_results = self.evaluate_single_epoch(epoch_number)
                self.write_log_line('Mean evaluation results follow:', also_print=True)
                self.write_log_line(pformat(mean_metric_results), also_print=True)
                rms_metrics = np.sqrt(np.mean(tuple(
                    mean_metric_results[metric_name]**2 for metric_name in mean_metric_results
                )))
                self.write_log_line('The RMS value is {:.4f}'.format(rms_metrics), also_print=True)
                for metric_name in mean_metric_results:
                    self.log_scalar('Evaluation/'+metric_name, mean_metric_results[metric_name], epoch_number)
                self.log_scalar('Evaluation/All_Metrics_RMS', rms_metrics, epoch_number)
                if best_rms_metrics is None: best_rms_metrics = rms_metrics
                if rms_metrics <= best_rms_metrics:
                    self.write_log_line('This is the new best epoch!!', also_print=True)
                    best_rms_metrics = rms_metrics              
                    self.write_log_line('Saving new best model...')
                    self.save(epoch_number, best=True)
                    saved = True
            if epoch_number % save_every == 0:
                self.write_log_line('This is a save epoch.')
                if saved:
                    self.write_log_line('However, the model has already been saved this epoch. Resuming training.')
                else:
                    self.write_log_line('Saving model...')
                    self.save(epoch_number, best=False)
                    self.write_log_line('Resuming training.', also_print=True)
            epoch_number += 1
            # consult the scheduler for learning rate change
            self.scheduler.step()
