# Standard library
from datetime import datetime, timedelta
import glob
import os

# Third-party
import earthkit.data
import imageio
import matplotlib.pyplot as plt
import numpy as np
import pytorch_lightning as pl
from pytorch_lightning.utilities import rank_zero_only
import torch
import wandb

# First-party
from neural_lam import config, metrics, utils, vis


class ARModel(pl.LightningModule):
    """
    Generic auto-regressive weather model.
    Abstract class that can be extended.
    """

    # pylint: disable=arguments-differ
    # Disable to override args/kwargs from superclass

    def __init__(self, args):
        super().__init__()
        self.save_hyperparameters()
        self.args = args
        self.config_loader = config.Config.from_file(args.data_config)

        # Load static features for grid/data
        static_data_dict = utils.load_static_data(
            self.config_loader.dataset.name
        )
        for static_data_name, static_data_tensor in static_data_dict.items():
            self.register_buffer(
                static_data_name, static_data_tensor, persistent=False
            )

        # Double grid output dim. to also output std.-dev.
        self.output_std = bool(args.output_std)
        if self.output_std:
            # Pred. dim. in grid cell
            self.grid_output_dim = 2 * self.config_loader.num_data_vars()
        else:
            # Pred. dim. in grid cell
            self.grid_output_dim = self.config_loader.num_data_vars()
            # Store constant per-variable std.-dev. weighting
            # Note that this is the inverse of the multiplicative weighting
            # in wMSE/wMAE
            self.register_buffer(
                "per_var_std",
                self.step_diff_std / torch.sqrt(self.param_weights),
                persistent=False,
            )

        # grid_dim from data + static
        (
            self.num_grid_nodes,
            grid_static_dim,
        ) = self.grid_static_features.shape
        self.grid_dim = (
            2 * self.config_loader.num_data_vars()
            + grid_static_dim
            + self.config_loader.dataset.num_forcing_features
        )

        # Instantiate loss function
        self.loss = metrics.get_metric(args.loss)

        # Pre-compute interior mask for use in loss function
        self.register_buffer(
            "interior_mask", 1.0 - self.border_mask, persistent=False
        )  # (num_grid_nodes, 1), 1 for non-border

        self.step_length = args.step_length  # Number of hours per pred. step
        self.val_metrics = {
            "mse": [],
        }
        self.test_metrics = {
            "mse": [],
            "mae": [],
        }
        if self.output_std:
            self.test_metrics["output_std"] = []  # Treat as metric

        # For making restoring of optimizer state optional
        self.restore_opt = args.restore_opt

        # For example plotting
        self.n_example_pred = args.n_example_pred
        self.plotted_examples = 0

        # For storing spatial loss maps during evaluation
        self.spatial_loss_maps = []

        self.inference_output = []
        "Storage for the output of individual inference steps"

        self.variable_indices = self.pre_compute_variable_indices()
        "Index mapping of variable names to their levels in the array."
        self.selected_vars_units = [
            (var_name, var_unit)
            for var_name, var_unit in zip(
                self.config_loader.dataset.var_names,
                self.config_loader.dataset.var_units,
            )
            if var_name in self.config_loader.dataset.eval_plot_vars
        ]

    def configure_optimizers(self):
        opt = torch.optim.AdamW(
            self.parameters(), lr=self.args.lr, betas=(0.9, 0.95)
        )
        return opt

    @property
    def interior_mask_bool(self):
        """
        Get the interior mask as a boolean (N,) mask.
        """
        return self.interior_mask[:, 0].to(torch.bool)

    def pre_compute_variable_indices(self):
        """
        Pre-compute indices for each variable in the input tensor
        """
        variable_indices = {}
        all_vars = []
        index = 0
        # Create a list of tuples for all variables, using level 0 for 2D
        # variables
        for var_name in self.config_loader.dataset.var_names:
            if self.config_loader.dataset.var_is_3d:
                for level in self.config_loader.dataset.vertical_levels:
                    all_vars.append((var_name, level))
            else:
                all_vars.append((var_name, 0))  # Use level 0 for 2D variables

        # Sort the variables based on the tuples
        sorted_vars = sorted(all_vars)

        for var in sorted_vars:
            var_name, level = var
            if var_name not in variable_indices:
                variable_indices[var_name] = []
            variable_indices[var_name].append(index)
            index += 1

        return variable_indices

    @staticmethod
    def expand_to_batch(x, batch_size):
        """
        Expand tensor with initial batch dimension
        """
        return x.unsqueeze(0).expand(batch_size, -1, -1)

    def single_prediction(self, prev_state, prev_prev_state, forcing):
        """
        Step state one step ahead using prediction model, X_{t-1}, X_t -> X_t+1
        prev_state: (B, num_grid_nodes, feature_dim), X_t
        prev_prev_state: (B, num_grid_nodes, feature_dim), X_{t-1}
        forcing: (B, num_grid_nodes, forcing_dim)
        """
        raise NotImplementedError("No prediction step implemented")

    def predict_step(self, batch, batch_idx):
        """
        Run the inference on batch.
        """
        prediction, target, pred_std = self.common_step(batch)

        # Compute all evaluation metrics for error maps
        # Note: explicitly list metrics here, as test_metrics can contain
        # additional ones, computed differently, but that should be aggregated
        # on_predict_epoch_end
        for metric_name in ("mse", "mae"):
            metric_func = metrics.get_metric(metric_name)
            batch_metric_vals = metric_func(
                prediction,
                target,
                pred_std,
                mask=self.interior_mask_bool,
                sum_vars=False,
            )  # (B, pred_steps, d_f)
            self.test_metrics[metric_name].append(batch_metric_vals)

        if self.output_std:
            # Store output std. per variable, spatially averaged
            mean_pred_std = torch.mean(
                pred_std[..., self.interior_mask_bool, :], dim=-2
            )  # (B, pred_steps, d_f)
            self.test_metrics["output_std"].append(mean_pred_std)

        # Save per-sample spatial loss for specific times
        spatial_loss = self.loss(
            prediction, target, pred_std, average_grid=False
        )  # (B, pred_steps, num_grid_nodes)
        log_spatial_losses = spatial_loss[
            :, [step - 1 for step in self.args.val_steps_to_log]
        ]
        self.spatial_loss_maps.append(log_spatial_losses)
        # (B, N_log, num_grid_nodes)

        if self.trainer.global_rank == 0:
            self.plot_examples(batch, batch_idx, prediction=prediction)
        self.inference_output.append(prediction)

    def unroll_prediction(self, init_states, forcing_features, true_states):
        """
        Roll out prediction taking multiple autoregressive steps with model
        init_states: (B, 2, num_grid_nodes, d_f)
        forcing_features: (B, pred_steps, num_grid_nodes, d_static_f)
        true_states: (B, pred_steps, num_grid_nodes, d_f)
        """
        prev_prev_state = init_states[:, 0]
        prev_state = init_states[:, 1]
        prediction_list = []
        pred_std_list = []
        pred_steps = forcing_features.shape[1]

        for i in range(pred_steps):
            forcing = forcing_features[:, i]
            border_state = true_states[:, i]

            pred_state, pred_std = self.single_prediction(
                prev_state, prev_prev_state, forcing
            )
            # state: (B, num_grid_nodes, d_f)
            # pred_std: (B, num_grid_nodes, d_f) or None

            # Overwrite border with true state
            new_state = (
                self.border_mask * border_state
                + self.interior_mask * pred_state
            )

            prediction_list.append(new_state)
            if self.output_std:
                pred_std_list.append(pred_std)

            # Update conditioning states
            prev_prev_state = prev_state
            prev_state = new_state

        prediction = torch.stack(
            prediction_list, dim=1
        )  # (B, pred_steps, num_grid_nodes, d_f)
        if self.output_std:
            pred_std = torch.stack(
                pred_std_list, dim=1
            )  # (B, pred_steps, num_grid_nodes, d_f)
        else:
            pred_std = self.per_var_std  # (d_f,)

        return prediction, pred_std

    def common_step(self, batch):
        """
        Predict on single batch
        batch consists of:
        init_states: (B, 2, num_grid_nodes, d_features)
        target_states: (B, pred_steps, num_grid_nodes, d_features)
        forcing_features: (B, pred_steps, num_grid_nodes, d_forcing),
            where index 0 corresponds to index 1 of init_states
        """
        (
            init_states,
            target_states,
            forcing_features,
        ) = batch

        prediction, pred_std = self.unroll_prediction(
            init_states, forcing_features, target_states
        )  # (B, pred_steps, num_grid_nodes, d_f)
        # prediction: (B, pred_steps, num_grid_nodes, d_f)
        # pred_std: (B, pred_steps, num_grid_nodes, d_f) or (d_f,)

        return prediction, target_states, pred_std

    def training_step(self, batch):
        """
        Train on single batch
        """
        prediction, target, pred_std = self.common_step(batch)

        # Compute loss
        batch_loss = torch.mean(
            self.loss(
                prediction, target, pred_std, mask=self.interior_mask_bool
            )
        )  # mean over unrolled times and batch

        log_dict = {"train_loss": batch_loss}
        self.log_dict(
            log_dict, prog_bar=True, on_step=True, on_epoch=True, sync_dist=True
        )
        return batch_loss

    def all_gather_cat(self, tensor_to_gather):
        """
        Gather tensors across all ranks, and concatenate across dim. 0
        (instead of stacking in new dim. 0)

        tensor_to_gather: (d1, d2, ...), distributed over K ranks

        returns: (K*d1, d2, ...)
        """
        return self.all_gather(tensor_to_gather).flatten(0, 1)

    # newer lightning versions requires batch_idx argument, even if unused
    # pylint: disable-next=unused-argument
    def validation_step(self, batch, batch_idx):
        """
        Run validation on single batch
        """
        prediction, target, pred_std = self.common_step(batch)

        time_step_loss = torch.mean(
            self.loss(
                prediction, target, pred_std, mask=self.interior_mask_bool
            ),
            dim=0,
        )  # (time_steps-1)
        mean_loss = torch.mean(time_step_loss)

        # Log loss per time step forward and mean
        val_log_dict = {
            f"val_loss_unroll{step}": time_step_loss[step - 1]
            for step in self.args.val_steps_to_log
        }
        val_log_dict["val_mean_loss"] = mean_loss
        self.log_dict(
            val_log_dict, on_step=False, on_epoch=True, sync_dist=True
        )

        # Store MSEs
        entry_mses = metrics.mse(
            prediction,
            target,
            pred_std,
            mask=self.interior_mask_bool,
            sum_vars=False,
        )  # (B, pred_steps, d_f)
        self.val_metrics["mse"].append(entry_mses)

    def on_validation_epoch_end(self):
        """
        Compute val metrics at the end of val epoch
        """
        # Create error maps for all test metrics
        self.aggregate_and_plot_metrics(self.val_metrics, prefix="val")

        # Clear lists with validation metrics values
        for metric_list in self.val_metrics.values():
            metric_list.clear()

    # pylint: disable-next=unused-argument
    def test_step(self, batch, batch_idx):
        """
        Run test on single batch
        """
        prediction, target, pred_std = self.common_step(batch)
        # prediction: (B, pred_steps, num_grid_nodes, d_f)
        # pred_std: (B, pred_steps, num_grid_nodes, d_f) or (d_f,)

        time_step_loss = torch.mean(
            self.loss(
                prediction, target, pred_std, mask=self.interior_mask_bool
            ),
            dim=0,
        )  # (time_steps-1,)
        mean_loss = torch.mean(time_step_loss)

        # Log loss per time step forward and mean
        test_log_dict = {
            f"test_loss_unroll{step}": time_step_loss[step - 1]
            for step in self.args.val_steps_to_log
        }
        test_log_dict["test_mean_loss"] = mean_loss

        self.log_dict(
            test_log_dict, on_step=False, on_epoch=True, sync_dist=True
        )

        # Compute all evaluation metrics for error maps
        # Note: explicitly list metrics here, as test_metrics can contain
        # additional ones, computed differently, but that should be aggregated
        # on_test_epoch_end
        for metric_name in ("mse", "mae"):
            metric_func = metrics.get_metric(metric_name)
            batch_metric_vals = metric_func(
                prediction,
                target,
                pred_std,
                mask=self.interior_mask_bool,
                sum_vars=False,
            )  # (B, pred_steps, d_f)
            self.test_metrics[metric_name].append(batch_metric_vals)

        if self.output_std:
            # Store output std. per variable, spatially averaged
            mean_pred_std = torch.mean(
                pred_std[..., self.interior_mask_bool, :], dim=-2
            )  # (B, pred_steps, d_f)
            self.test_metrics["output_std"].append(mean_pred_std)

        # Save per-sample spatial loss for specific times
        spatial_loss = self.loss(
            prediction, target, pred_std, average_grid=False
        )  # (B, pred_steps, num_grid_nodes)
        log_spatial_losses = spatial_loss[
            :, [step - 1 for step in self.args.val_steps_to_log]
        ]
        self.spatial_loss_maps.append(log_spatial_losses)
        # (B, N_log, num_grid_nodes)

        # Plot example predictions (on rank 0 only)
        if (
            self.trainer.is_global_zero
            and self.plotted_examples < self.n_example_pred
        ):
            # Need to plot more example predictions
            n_additional_examples = min(
                prediction.shape[0], self.n_example_pred - self.plotted_examples
            )

            self.plot_examples(
                batch, n_additional_examples, prediction=prediction
            )

    @rank_zero_only
    def plot_examples(self, batch, n_examples, batch_idx: int, prediction=None):
        """
        Plot the first n_examples forecasts from batch.

        The function checks for the presence of test_dataset or
        predict_dataset within the trainer's data module,
        handles indexing within the batch for targeted analysis,
        performs prediction rescaling, and plots results.

        Parameters:
        - batch: batch with data to plot corresponding forecasts for
        - n_examples: number of forecasts to plot
        - batch_idx (int): index of the batch being processed
        - prediction: (B, pred_steps, num_grid_nodes, d_f), existing prediction.
                Generate if None.
        """
        if prediction is None:
            prediction, target = self.common_step(batch)

        target = batch[1]

        # Determine the dataset to work with (test_dataset or predict_dataset)
        dataset = None
        if (
            hasattr(self.trainer.datamodule, "test_dataset")
            and self.trainer.datamodule.test_dataset
        ):
            dataset = self.trainer.datamodule.test_dataset
            plot_name = "test"
        elif (
            hasattr(self.trainer.datamodule, "predict_dataset")
            and self.trainer.datamodule.predict_dataset
        ):
            dataset = self.trainer.datamodule.predict_dataset
            plot_name = "prediction"

        if (
            dataset
            and self.trainer.global_rank == 0
            and dataset.batch_index == batch_idx
        ):
            index_within_batch = dataset.index_within_batch

        # Rescale to original data scale
        prediction_rescaled = prediction * self.data_std + self.data_mean
        target_rescaled = target * self.data_std + self.data_mean

        # Iterate over the examples
        for pred_slice, target_slice in zip(
            prediction_rescaled[:n_examples], target_rescaled[:n_examples]
        ):
            # Each slice is (pred_steps, num_grid_nodes, d_f)
            self.plotted_examples += 1  # Increment already here

            var_vmin = (
                torch.minimum(
                    pred_slice.flatten(0, 1).min(dim=0)[0],
                    target_slice.flatten(0, 1).min(dim=0)[0],
                )
                .cpu()
                .numpy()
            )  # (d_f,)
            var_vmax = (
                torch.maximum(
                    pred_slice.flatten(0, 1).max(dim=0)[0],
                    target_slice.flatten(0, 1).max(dim=0)[0],
                )
                .cpu()
                .numpy()
            )  # (d_f,)
            var_vranges = list(zip(var_vmin, var_vmax))

            # Iterate over prediction horizon time steps
            for t_i, (pred_t, target_t) in enumerate(
                zip(pred_slice, target_slice), start=1
            ):
                # Create one figure per variable at this time step
                var_figs = [
                    vis.plot_prediction(
                        pred_t[:, var_i],
                        target_t[:, var_i],
                        self.interior_mask[:, 0],
                        self.config_loader,
                        title=f"{var_name} ({var_unit}), "
                        f"t={t_i} ({self.step_length * t_i} h)",
                        vrange=var_vrange,
                    )
                    for var_i, (var_name, var_unit, var_vrange) in enumerate(
                        zip(
                            self.config_loader.dataset.var_names,
                            self.config_loader.dataset.var_units,
                            var_vranges,
                        )
                    )
                ]

                example_i = self.plotted_examples
                wandb.log(
                    {
                        f"{var_name}_{plot_name}_{example_i}": wandb.Image(fig)
                        for var_name, fig in zip(
                            self.config_loader.dataset.var_names, var_figs
                        )
                    }
                )
                plt.close(
                    "all"
                )  # Close all figs for this time step, saves memory

            # Save pred and target as .pt files
            torch.save(
                pred_slice.cpu(),
                os.path.join(
                    wandb.run.dir, f"example_pred_{self.plotted_examples}.pt"
                ),
            )
            torch.save(
                target_slice.cpu(),
                os.path.join(
                    wandb.run.dir, f"example_target_{self.plotted_examples}.pt"
                ),
            )

    def create_metric_log_dict(self, metric_tensor, prefix, metric_name):
        """
        Put together a dict with everything to log for one metric.
        Also saves plots as pdf and csv if using test prefix.

        metric_tensor: (pred_steps, d_f), metric values per time and variable
        prefix: string, prefix to use for logging
        metric_name: string, name of the metric

        Return:
        log_dict: dict with everything to log for given metric
        """
        log_dict = {}
        metric_fig = vis.plot_error_map(
            metric_tensor, self.config_loader, step_length=self.step_length
        )
        full_log_name = f"{prefix}_{metric_name}"
        log_dict[full_log_name] = wandb.Image(metric_fig)

        if prefix == "test":
            # Save pdf
            metric_fig.savefig(
                os.path.join(wandb.run.dir, f"{full_log_name}.pdf")
            )
            # Save errors also as csv
            np.savetxt(
                os.path.join(wandb.run.dir, f"{full_log_name}.csv"),
                metric_tensor.cpu().numpy(),
                delimiter=",",
            )

        # Check if metrics are watched, log exact values for specific vars
        if full_log_name in self.args.metrics_watch:
            for var_i, timesteps in self.args.var_leads_metrics_watch.items():
                var = self.config_loader.dataset.var_nums[var_i]
                log_dict.update(
                    {
                        f"{full_log_name}_{var}_step_{step}": metric_tensor[
                            step - 1, var_i
                        ]  # 1-indexed in data_config
                        for step in timesteps
                    }
                )

        return log_dict

    def aggregate_and_plot_metrics(self, metrics_dict, prefix):
        """
        Aggregate and create error map plots for all metrics in metrics_dict

        metrics_dict: dictionary with metric_names and list of tensors
            with step-evals.
        prefix: string, prefix to use for logging
        """
        log_dict = {}
        for metric_name, metric_val_list in metrics_dict.items():
            metric_tensor = self.all_gather_cat(
                torch.cat(metric_val_list, dim=0)
            )  # (N_eval, pred_steps, d_f)

            if self.trainer.is_global_zero:
                metric_tensor_averaged = torch.mean(metric_tensor, dim=0)
                # (pred_steps, d_f)

                # Take square root after all averaging to change MSE to RMSE
                if "mse" in metric_name:
                    metric_tensor_averaged = torch.sqrt(metric_tensor_averaged)
                    metric_name = metric_name.replace("mse", "rmse")

                # Note: we here assume rescaling for all metrics is linear
                metric_rescaled = metric_tensor_averaged * self.data_std
                # (pred_steps, d_f)
                log_dict.update(
                    self.create_metric_log_dict(
                        metric_rescaled, prefix, metric_name
                    )
                )

        if self.trainer.is_global_zero and not self.trainer.sanity_checking:
            wandb.log(log_dict)  # Log all
            plt.close("all")  # Close all figs

    def on_test_epoch_end(self):
        """
        Compute test metrics and make plots at the end of test epoch.
        Will gather stored tensors and perform plotting and logging on rank 0.
        """
        # Create error maps for all test metrics
        self.aggregate_and_plot_metrics(self.test_metrics, prefix="test")

        # Plot spatial loss maps
        spatial_loss_tensor = self.all_gather_cat(
            torch.cat(self.spatial_loss_maps, dim=0)
        )  # (N_test, N_log, num_grid_nodes)
        if self.trainer.is_global_zero:
            mean_spatial_loss = torch.mean(
                spatial_loss_tensor, dim=0
            )  # (N_log, num_grid_nodes)

            loss_map_figs = [
                vis.plot_spatial_error(
                    loss_map,
                    self.interior_mask[:, 0],
                    self.config_loader,
                    title=f"Test loss, t={t_i} ({self.step_length * t_i} h)",
                )
                for t_i, loss_map in zip(
                    self.args.val_steps_to_log, mean_spatial_loss
                )
            ]

            # log all to same wandb key, sequentially
            for fig in loss_map_figs:
                wandb.log({"test_loss": wandb.Image(fig)})

            # also make without title and save as pdf
            pdf_loss_map_figs = [
                vis.plot_spatial_error(
                    loss_map, self.interior_mask[:, 0], self.config_loader
                )
                for loss_map in mean_spatial_loss
            ]
            pdf_loss_maps_dir = os.path.join(wandb.run.dir, "spatial_loss_maps")
            os.makedirs(pdf_loss_maps_dir, exist_ok=True)
            for t_i, fig in zip(self.args.val_steps_to_log, pdf_loss_map_figs):
                fig.savefig(os.path.join(pdf_loss_maps_dir, f"loss_t{t_i}.pdf"))
            # save mean spatial loss as .pt file also
            torch.save(
                mean_spatial_loss.cpu(),
                os.path.join(wandb.run.dir, "mean_spatial_loss.pt"),
            )

        self.spatial_loss_maps.clear()

    @rank_zero_only
    def on_predict_epoch_end(self):
        """
        Compute test metrics and make plots at the end of test epoch.
        Will gather stored tensors and perform plotting and logging on rank 0.
        """

        plot_dir_path = f"{wandb.run.dir}/media/images"
        value_dir_path = f"{wandb.run.dir}/results/inference"
        # Ensure the directory for saving numpy arrays exists
        os.makedirs(plot_dir_path, exist_ok=True)
        os.makedirs(value_dir_path, exist_ok=True)

        # For values
        for i, prediction in enumerate(self.inference_output):

            # Rescale to original data scale
            prediction_rescaled = prediction * self.data_std + self.data_mean

            # Process and save the prediction
            prediction_array = prediction_rescaled.cpu().numpy()
            file_path = os.path.join(value_dir_path, f"prediction_{i}.npy")
            np.save(file_path, prediction_array)
            self.save_pred_as_grib(file_path, value_dir_path)

        dir_path = f"{wandb.run.dir}/media/images"
        for var_name, _ in self.selected_vars_units:
            var_indices = self.variable_indices[var_name]
            for lvl_i, _ in enumerate(var_indices):
                # Calculate var_vrange for each index
                lvl = self.config_loader.dataset.vertical_levels[lvl_i]

                # Get all the images for the current variable and index
                images = sorted(
                    glob.glob(
                        f"{dir_path}/{var_name}_test_lvl_{lvl:02}_t_*.png"
                    )
                )
                # Generate the GIF
                with imageio.get_writer(
                    f"{dir_path}/{var_name}_lvl_{lvl:02}.gif",
                    mode="I",
                    fps=1,
                ) as writer:
                    for filename in images:
                        image = imageio.imread(filename)
                        writer.append_data(image)

        self.spatial_loss_maps.clear()

    def _generate_time_steps(self):
        """Generate a list with all time steps in inference."""
        # Parse the times
        base_time = self.config_loader.dataset.eval_datetime[0]

        if isinstance(base_time, str):
            base_time = datetime.strptime(base_time, "%Y%m%d%H")
        time_steps = {}
        # Generate dates for each step
        for i in range(self.config_loader.dataset.eval_horizon - 2):
            # Compute the new date by adding the step interval in hours - 3
            new_date = base_time + timedelta(hours=i * self.config_loader.dataset.train_horizon)
            # Format the date back
            time_steps[i] = new_date.strftime("%Y%m%d%H")

    def save_pred_as_grib(self, file_path: str, value_dir_path: str):
        """Save the prediction values into GRIB format."""
        # Initialize the lists to loop over
        indices = self.precompute_variable_indices()
        time_steps = self._generate_time_steps()
        # Loop through all the time steps and all the variables
        for time_idx, date_str in time_steps.items():
            # Initialize final data object
            final_data = earthkit.data.FieldList()
            for variable, grib_code in self.config_loader.dataset.grib_names.items():
                # here find the key of the cariable in constants.is_3D
                #  and if == 7, assign a cut of 7 on the reshape. Else 1
                if self.config_loader.dataset.var_is_3d[variable]:
                    shape_val = len(self.config_loader.dataset.vertical_levels)
                    vertical = self.config_loader.dataset.vertical_levels
                else:
                    # Special handling for T_2M and *_10M variables
                    if variable == "T_2M":
                        shape_val = 1
                        vertical = 2
                    elif variable.endswith("_10M"):
                        shape_val = 1
                        vertical = 10
                    else:
                        shape_val = 1
                        vertical = 0
                # Find the value range to sample
                value_range = indices[variable]

                sample_file = self.config_loader.dataset.sample_grib
                if variable == "RELHUM":
                    variable = "r"
                    sample_file = self.config_loader.dataset.sample_z_grib

                # Load the sample grib file
                original_data = earthkit.data.from_source("file", sample_file)

                subset = original_data.sel(shortName=grib_code, level=vertical)
                md = subset.metadata()

                # Cut the datestring into date and time and then override all
                # values in md
                date = date_str[:8]
                time = date_str[8:]

                for index, item in enumerate(md):
                    md[index] = item.override({"date": date}).override(
                        {"time": time}
                    )
                if len(md) > 0:
                    # Load the array to replace the values with
                    replacement_data = np.load(file_path)
                    original_cut = replacement_data[
                        0, time_idx, :, min(value_range) : max(value_range) + 1
                    ].reshape(
                        self.config_loader.dataset.grib_shape_state[1],
                        self.config_loader.dataset.grib_shape_state[0],
                        shape_val,
                    )
                    cut_values = np.moveaxis(
                        original_cut, [-3, -2, -1], [-1, -2, -3]
                    )
                    # Can we stack Fieldlists?
                    data_new = earthkit.data.FieldList.from_array(
                        cut_values, md
                    )
                    final_data += data_new
            # Create the modified GRIB file with the predicted data
            grib_path = os.path.join(
                value_dir_path, f"prediction_{date_str}_grib"
            )
            final_data.save(grib_path)

    def on_load_checkpoint(self, checkpoint):
        """
        Perform any changes to state dict before loading checkpoint
        """
        loaded_state_dict = checkpoint["state_dict"]

        # Fix for loading older models after IneractionNet refactoring, where
        # the grid MLP was moved outside the encoder InteractionNet class
        if "g2m_gnn.grid_mlp.0.weight" in loaded_state_dict:
            replace_keys = list(
                filter(
                    lambda key: key.startswith("g2m_gnn.grid_mlp"),
                    loaded_state_dict.keys(),
                )
            )
            for old_key in replace_keys:
                new_key = old_key.replace(
                    "g2m_gnn.grid_mlp", "encoding_grid_mlp"
                )
                loaded_state_dict[new_key] = loaded_state_dict[old_key]
                del loaded_state_dict[old_key]
        if not self.restore_opt:
            opt = self.configure_optimizers()
            checkpoint["optimizer_states"] = [opt.state_dict()]
