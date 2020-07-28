import gym
import numpy as np
import torch
from torch.nn import functional as F

from models.decoder import StateTransitionDecoder, RewardDecoder, TaskDecoder
from models.encoder import RNNEncoder
from utils.storage_vae import RolloutStorageVAE
from utils.helpers import get_task_dim, get_num_tasks

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class VaribadVAE:
    """
    VAE of VariBAD:
    - has an encoder and decoder
    - can compute the ELBO loss
    - can update the VAE (encoder+decoder)
    """
    def __init__(self, args, logger, get_iter_idx):

        self.args = args
        self.logger = logger
        self.get_iter_idx = get_iter_idx
        self.task_dim = get_task_dim(self.args)
        self.num_tasks = get_num_tasks(self.args)

        # initialise the encoder
        self.encoder = self.initialise_encoder()

        # initialise the decoders (returns None for unused decoders)
        self.state_decoder, self.reward_decoder, self.task_decoder = self.initialise_decoder()

        # initialise rollout storage for the VAE update
        # (this differs from the data that the on-policy RL algorithm uses)
        self.rollout_storage = RolloutStorageVAE(num_processes=self.args.num_processes,
                                                 max_trajectory_len=self.args.max_trajectory_len,
                                                 zero_pad=True,
                                                 max_num_rollouts=self.args.size_vae_buffer,
                                                 obs_dim=self.args.obs_dim,
                                                 action_dim=self.args.action_dim,
                                                 vae_buffer_add_thresh=self.args.vae_buffer_add_thresh,
                                                 task_dim=self.task_dim,
                                                 )

        # initalise optimiser for the encoder and decoders
        decoder_params = []
        if not self.args.disable_decoder:
            if self.args.decode_reward:
                decoder_params.extend(self.reward_decoder.parameters())
            if self.args.decode_state:
                decoder_params.extend(self.state_decoder.parameters())
            if self.args.decode_task:
                decoder_params.extend(self.task_decoder.parameters())
        self.optimiser_vae = torch.optim.Adam([*self.encoder.parameters(), *decoder_params], lr=self.args.lr_vae)

    def initialise_encoder(self):
        """ Initialises and returns an RNN encoder """
        encoder = RNNEncoder(
            layers_before_gru=self.args.layers_before_aggregator,
            hidden_size=self.args.aggregator_hidden_size,
            layers_after_gru=self.args.layers_after_aggregator,
            latent_dim=self.args.latent_dim,
            action_dim=self.args.action_dim,
            action_embed_dim=self.args.action_embedding_size,
            state_dim=self.args.obs_dim,
            state_embed_dim=self.args.state_embedding_size,
            reward_size=1,
            reward_embed_size=self.args.reward_embedding_size,
        ).to(device)
        return encoder

    def initialise_decoder(self):
        """ Initialises and returns the (state/reward/task) decoder as specified in self.args """

        if self.args.disable_decoder:
            return None, None, None

        latent_dim = self.args.latent_dim
        # double latent dimension (input size to decoder) if we use a deterministic latents (for easier comparison)
        if self.args.disable_stochasticity_in_latent:
            latent_dim *= 2

        # initialise state decoder for VAE
        if self.args.decode_state:
            state_decoder = StateTransitionDecoder(
                layers=self.args.state_decoder_layers,
                latent_dim=latent_dim,
                action_dim=self.args.action_dim,
                action_embed_dim=self.args.action_embedding_size,
                state_dim=self.args.obs_dim,
                state_embed_dim=self.args.state_embedding_size,
                pred_type=self.args.state_pred_type,
            ).to(device)
        else:
            state_decoder = None

        # initialise reward decoder for VAE
        if self.args.decode_reward:
            reward_decoder = RewardDecoder(
                layers=self.args.reward_decoder_layers,
                latent_dim=latent_dim,
                state_dim=self.args.obs_dim,
                state_embed_dim=self.args.state_embedding_size,
                action_dim=self.args.action_dim,
                action_embed_dim=self.args.action_embedding_size,
                num_states=self.args.num_states,
                multi_head=self.args.multihead_for_reward,
                pred_type=self.args.rew_pred_type,
                input_prev_state=self.args.input_prev_state,
                input_action=self.args.input_action,
            ).to(device)
        else:
            reward_decoder = None

        # initialise task decoder for VAE
        if self.args.decode_task:
            task_decoder = TaskDecoder(
                latent_dim=latent_dim,
                layers=self.args.task_decoder_layers,
                task_dim=self.task_dim,
                num_tasks=self.num_tasks,
                pred_type=self.args.task_pred_type,
            ).to(device)
        else:
            task_decoder = None

        return state_decoder, reward_decoder, task_decoder

    def compute_state_reconstruction_loss(self, latent, prev_obs, next_obs, action, return_predictions=False):
        """ Compute state reconstruction loss.
        (No reduction of loss along batch dimension is done here; sum/avg has to be done outside) """

        state_pred = self.state_decoder(latent, prev_obs, action)

        if self.args.state_pred_type == 'deterministic':
            loss_state = (state_pred - next_obs).pow(2).mean(dim=-1)
        elif self.args.state_pred_type == 'gaussian':  # TODO: untested!
            state_pred_mean = state_pred[:, :state_pred.shape[1] // 2]
            state_pred_std = torch.exp(0.5 * state_pred[:, state_pred.shape[1] // 2:])
            m = torch.distributions.normal.Normal(state_pred_mean, state_pred_std)
            loss_state = -m.log_prob(next_obs).mean(dim=-1)
        else:
            raise NotImplementedError

        if return_predictions:
            return loss_state, state_pred
        else:
            return loss_state

    def compute_rew_reconstruction_loss(self, latent, prev_obs, next_obs, action, reward, return_predictions=False):
        """ Compute reward reconstruction loss.
        (No reduction of loss along batch dimension is done here; sum/avg has to be done outside) """

        if self.args.multihead_for_reward:

            rew_pred = self.reward_decoder(latent, None)
            if self.args.rew_pred_type == 'categorical':
                rew_pred = F.softmax(rew_pred, dim=-1)
            elif self.args.rew_pred_type == 'bernoulli':
                rew_pred = torch.sigmoid(rew_pred)

            env = gym.make(self.args.env_name)
            state_indices = env.task_to_id(next_obs).to(device)
            if state_indices.dim() < rew_pred.dim():
                state_indices = state_indices.unsqueeze(-1)
            rew_pred = rew_pred.gather(dim=-1, index=state_indices)
            rew_target = (reward == 1).float()
            if self.args.rew_pred_type == 'deterministic':  # TODO: untested!
                loss_rew = (rew_pred - reward).pow(2).mean(dim=-1)
            elif self.args.rew_pred_type in ['categorical', 'bernoulli']:
                loss_rew = F.binary_cross_entropy(rew_pred, rew_target, reduction='none').mean(dim=-1)
            else:
                raise NotImplementedError
        else:
            rew_pred = self.reward_decoder(latent, next_obs, prev_obs, action.float())
            rew_target = (rew_pred == 1).float()
            if self.args.rew_pred_type == 'bernoulli':
                loss_rew = F.binary_cross_entropy(rew_pred, rew_target, reduction='none').mean(dim=-1)
            elif self.args.rew_pred_type == 'deterministic':
                loss_rew = (rew_pred - reward).pow(2).mean(dim=-1)
            else:
                raise NotImplementedError

        if return_predictions:
            return loss_rew, rew_pred
        else:
            return loss_rew

    def compute_task_reconstruction_loss(self, latent, task, return_predictions=False):
        """ Compute task reconstruction loss.
        (No reduction of loss along batch dimension is done here; sum/avg has to be done outside) """

        task_pred = self.task_decoder(latent)

        if self.args.task_pred_type == 'task_id':
            env = gym.make(self.args.env_name)
            task_target = env.task_to_id(task).to(device)
            # expand along first axis (number of ELBO terms)
            task_target = task_target.expand(task_pred.shape[:-1])
            loss_task = F.cross_entropy(task_pred, task_target, reduction='none')
        elif self.args.task_pred_type == 'task_description':
            loss_task = (task_pred - task).pow(2).mean(dim=-1)
        else:
            raise NotImplementedError

        if return_predictions:
            return loss_task, task_pred
        else:
            return loss_task

    def compute_kl_loss(self, latent_mean, latent_logvar, len_encoder):
        # -- KL divergence
        if self.args.kl_to_gauss_prior:
            kl_divergences = (- 0.5 * (1 + latent_logvar - latent_mean.pow(2) - latent_logvar.exp()).sum(dim=-1))
        else:
            gauss_dim = latent_mean.shape[-1]
            # add the gaussian prior
            all_means = torch.cat((torch.zeros(1, *latent_mean.shape[1:]).to(device), latent_mean))
            all_logvars = torch.cat((torch.zeros(1, *latent_logvar.shape[1:]).to(device), latent_logvar))
            # https://arxiv.org/pdf/1811.09975.pdf
            # KL(N(mu,E)||N(m,S)) = 0.5 * (log(|S|/|E|) - K + tr(S^-1 E) + (m-mu)^T S^-1 (m-mu)))
            mu = all_means[1:]
            m = all_means[:-1]
            logE = all_logvars[1:]
            logS = all_logvars[:-1]
            kl_divergences = 0.5 * (torch.sum(logS, dim=-1) - torch.sum(logE, dim=-1) - gauss_dim + torch.sum(
                1 / torch.exp(logS) * torch.exp(logE), dim=-1) + ((m - mu) / torch.exp(logS) * (m - mu)).sum(dim=-1))

        if self.args.learn_prior:
            mask = torch.ones(len(kl_divergences))
            mask[0] = 0
            kl_divergences = kl_divergences * mask

        # returns, for each ELBO_t term, one KL (so H+1 kl's)
        if len_encoder is not None:
            return kl_divergences[len_encoder]
        else:
            return kl_divergences

    def sum_reconstruction_terms(self, losses, idx_traj, len_encoder, trajectory_lens):

        """ Sums the reconstruction errors along episode horizon """
        if len(np.unique(trajectory_lens)) == 1 and not self.args.decode_only_past:
            # if for each embedding we decode the entire trajectory, we have a matrix and can sum along dim 1
            losses = losses.sum(dim=1)
        else:
            # otherwise, we loop and sum along the trajectory which we decoded (sum in ELBO_t)
            start_idx = 0
            partial_reconstruction_loss = []
            for i, idx_timestep in enumerate(len_encoder[idx_traj]):
                if self.args.decode_only_past:
                    dec_from = 0
                    dec_until = idx_timestep
                else:
                    dec_from = 0
                    dec_until = trajectory_lens[idx_traj]
                end_idx = start_idx + (dec_until - dec_from)
                if end_idx - start_idx != 0:
                    partial_reconstruction_loss.append(losses[start_idx:end_idx].sum())
                start_idx = end_idx
            losses = torch.stack(partial_reconstruction_loss)
        return losses

    def compute_loss(self, latent_mean, latent_logvar, vae_prev_obs, vae_next_obs, vae_actions,
                     vae_rewards, vae_tasks, trajectory_lens):

        assert (len(np.unique(trajectory_lens)) == 1) and not self.args.decode_only_past

        # take one sample for each ELBO term
        if not self.args.disable_stochasticity_in_latent:
            latent_samples = self.encoder._sample_gaussian(latent_mean, latent_logvar)
        else:
            latent_samples = torch.cat((latent_mean, latent_logvar), dim=-1)

        n_elbos = latent_mean.shape[0]  # includes the prior
        n_horizon = np.unique(trajectory_lens)[0]

        # expand the latent (to match the number of state/rew/action inputs to the decoder)
        # shape will be: [num tasks in batch] x [num elbos] x [len trajectory (reconstrution loss)] x [dimension]
        dec_embedding = latent_samples.unsqueeze(0).expand((n_horizon, *latent_samples.shape)).transpose(1, 0)

        # expand the state/rew/action inputs to the decoder (to match size of latents)
        # shape will be: [num tasks in batch] x [num elbos] x [len trajectory (reconstrution loss)] x [dimension]
        dec_prev_obs = vae_prev_obs.unsqueeze(0).expand((n_elbos, *vae_prev_obs.shape))
        dec_next_obs = vae_next_obs.unsqueeze(0).expand((n_elbos, *vae_next_obs.shape))
        dec_actions = vae_actions.unsqueeze(0).expand((n_elbos, *vae_actions.shape))
        dec_rewards = vae_rewards.unsqueeze(0).expand((n_elbos, *vae_rewards.shape))

        if self.args.decode_reward:
            # compute reconstruction loss for this trajectory (for each timestep that was encoded, decode everything and sum it up)
            # shape: [num_elbo_terms] x [num_reconstruction_terms] x [num_trajectories]
            rew_reconstruction_loss = self.compute_rew_reconstruction_loss(dec_embedding, dec_prev_obs, dec_next_obs,
                                                                           dec_actions, dec_rewards)
            # sum the reconstruction terms per elbo; sum the elbos, average across tasks
            rew_reconstruction_loss = rew_reconstruction_loss.sum(dim=0).sum(dim=0).mean()
        else:
            rew_reconstruction_loss = 0

        if self.args.decode_state:
            state_reconstruction_loss = self.compute_state_reconstruction_loss(dec_embedding, dec_prev_obs,
                                                                               dec_next_obs, dec_actions)
            # sum the reconstruction terms per elbo; sum the elbos, average across tasks
            state_reconstruction_loss = state_reconstruction_loss.sum(dim=0).sum(dim=-0).mean()
        else:
            state_reconstruction_loss = 0

        if self.args.decode_task:
            task_reconstruction_loss = self.compute_task_reconstruction_loss(latent_samples, vae_tasks)
            # sum the elbos, average across tasks
            task_reconstruction_loss = task_reconstruction_loss.sum(dim=0).mean()
        else:
            task_reconstruction_loss = 0

        if not self.args.disable_stochasticity_in_latent:
            # compute the KL term for each ELBO term of the current trajectory
            kl_loss = self.compute_kl_loss(latent_mean, latent_logvar, None)
            # sum the elbos, average across tasks
            kl_loss = kl_loss.sum(dim=0).mean()
        else:
            kl_loss = 0

        return rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, kl_loss

    def compute_loss_split_batches_by_task(self, latent_mean, latent_logvar, vae_prev_obs, vae_next_obs, vae_actions,
                                           vae_rewards, vae_tasks, trajectory_lens, len_encoder):

        """
        Loop over tasks to compute losses per task.
        Saves some memory if batch sizes are very large.
        """

        rew_reconstruction_loss = []
        state_reconstruction_loss = []
        task_reconstruction_loss = []
        kl_loss = []

        num_tasks = len(trajectory_lens)
        # for each task we have in our batch...
        for idx_traj in range(num_tasks):

            # get the embedding values (size: traj_length+1 * latent_dim; the +1 is for the prior)
            curr_means = latent_mean[:trajectory_lens[idx_traj] + 1, idx_traj, :]
            curr_logvars = latent_logvar[:trajectory_lens[idx_traj] + 1, idx_traj, :]
            # take one sample for each elbo term
            if not self.args.disable_stochasticity_in_latent:
                curr_samples = self.encoder._sample_gaussian(curr_means, curr_logvars)
            else:
                curr_samples = torch.cat((latent_mean, latent_logvar))

            # select data from current rollout (result is traj_length * obs_dim)
            curr_prev_obs = vae_prev_obs[:, idx_traj, :]
            curr_next_obs = vae_next_obs[:, idx_traj, :]
            curr_actions = vae_actions[:, idx_traj, :]
            curr_rewards = vae_rewards[:, idx_traj, :]

            # if the size of what we decode is always the same, we can speed up creating the batches
            if len(np.unique(trajectory_lens)) == 1 and not self.args.decode_only_past:

                num_latents = curr_samples.shape[0]  # includes the prior
                num_decodes = curr_prev_obs.shape[0]

                # expand the latent to match the (x, y) pairs of the decoder
                dec_embedding = curr_samples.unsqueeze(0).expand((num_decodes, *curr_samples.shape)).transpose(1, 0)
                dec_embedding_task = curr_samples

                # expand the (x, y) pair of the encoder
                dec_prev_obs = curr_prev_obs.unsqueeze(0).expand((num_latents, *curr_prev_obs.shape))
                dec_next_obs = curr_next_obs.unsqueeze(0).expand((num_latents, *curr_next_obs.shape))
                dec_actions = curr_actions.unsqueeze(0).expand((num_latents, *curr_actions.shape))
                dec_rewards = curr_rewards.unsqueeze(0).expand((num_latents, *curr_rewards.shape))

            # otherwise, we unfortunately have to loop!
            # loop through the lengths we are feeding into the encoder for that trajectory (starting with prior)
            # (these are the different ELBO_t terms)
            else:

                dec_embedding = []
                dec_embedding_task = []
                dec_prev_obs, dec_next_obs, dec_actions, dec_rewards = [], [], [], []

                for i, idx_timestep in enumerate(len_encoder[idx_traj]):

                    # get the index until which we want to decode
                    # (i.e. either until curr timestep or entire trajectory including future)
                    if self.args.decode_only_past:
                        dec_from = 0
                        dec_until = idx_timestep
                    else:
                        dec_from = 0
                        dec_until = trajectory_lens[idx_traj]

                    if dec_until != 0:
                        # (1) ... get the latent sample after feeding in some data (determined by len_encoder) & expand (to number of outputs)
                        # # num latent samples x embedding size
                        if not self.args.disable_stochasticity_in_latent:
                            dec_embedding.append(curr_samples[i].expand(dec_until - dec_from, -1))
                            dec_embedding_task.append(curr_samples[i])
                        else:
                            dec_embedding.append(
                                torch.cat((curr_means[idx_timestep], curr_logvars[idx_timestep])).expand(
                                    dec_until - dec_from, -1))
                            dec_embedding_task.append(torch.cat((curr_means[idx_timestep], curr_logvars[idx_timestep])))
                        # (2) ... get the predictions for the trajectory until the timestep we're interested in
                        dec_prev_obs.append(curr_prev_obs[dec_from:dec_until])
                        dec_next_obs.append(curr_next_obs[dec_from:dec_until])
                        dec_actions.append(curr_actions[dec_from:dec_until])
                        dec_rewards.append(curr_rewards[dec_from:dec_until])

                # stack all of the things we decode! the dimensions of these will be:
                # number of elbo terms (current timesteps from which we want to decode (H+1)
                # x
                # number of terms in elbo (reconstr. of traj.) (H)
                # x
                # dimension (of latent space or obs/act/rew)
                #
                # what we want to do is SUM across the length of the predicted trajectory and AVERAGE across the rest
                if len(np.unique(trajectory_lens)) == 1 and not self.args.decode_only_past:
                    dec_embedding = torch.stack(dec_embedding)
                    dec_embedding_task = torch.stack(dec_embedding_task)
                    #
                    dec_prev_obs = torch.stack(dec_prev_obs)
                    dec_next_obs = torch.stack(dec_next_obs)
                    dec_actions = torch.stack(dec_actions)
                    dec_rewards = torch.stack(dec_rewards)
                else:
                    dec_embedding = torch.cat(dec_embedding)
                    dec_embedding_task = torch.cat(dec_embedding_task)
                    #
                    dec_prev_obs = torch.cat(dec_prev_obs)
                    dec_next_obs = torch.cat(dec_next_obs)
                    dec_actions = torch.cat(dec_actions)
                    dec_rewards = torch.cat(dec_rewards)

            if self.args.decode_reward:
                # compute reconstruction loss for this trajectory (for each timestep that was encoded, decode everything and sum it up)
                # size: if all trajectories are of same length [num_elbo_terms x num_reconstruction_terms], otherwise it's flattened into one
                rrc = self.compute_rew_reconstruction_loss(dec_embedding, dec_prev_obs, dec_next_obs, dec_actions,
                                                           dec_rewards)
                rrc = self.sum_reconstruction_terms(rrc, idx_traj, len_encoder, trajectory_lens)
                rew_reconstruction_loss.append(rrc)

            if self.args.decode_state:
                src = self.compute_state_reconstruction_loss(dec_embedding, dec_prev_obs, dec_next_obs, dec_actions)
                src = self.sum_reconstruction_terms(src, idx_traj, len_encoder, trajectory_lens)
                state_reconstruction_loss.append(src)

            if self.args.decode_task:
                trc = self.compute_task_reconstruction_loss(dec_embedding_task, vae_tasks[idx_traj])
                task_reconstruction_loss.append(trc)

            if not self.args.disable_stochasticity_in_latent:
                # compute the KL term for each ELBO term of the current trajectory
                kl = self.compute_kl_loss(curr_means, curr_logvars, len_encoder[idx_traj])
                kl_loss.append(kl)

        # sum the ELBO_t terms
        if self.args.decode_reward:
            rew_reconstruction_loss = torch.stack(rew_reconstruction_loss)
            rew_reconstruction_loss = rew_reconstruction_loss.sum(dim=1)
        else:
            rew_reconstruction_loss = 0

        if self.args.decode_state:
            state_reconstruction_loss = torch.stack(state_reconstruction_loss)
            state_reconstruction_loss = state_reconstruction_loss.sum(dim=1)
        else:
            state_reconstruction_loss = 0

        if self.args.decode_task:
            task_reconstruction_loss = torch.stack(task_reconstruction_loss)
            task_reconstruction_loss = task_reconstruction_loss.sum(dim=1)
        else:
            task_reconstruction_loss = 0

        if not self.args.disable_stochasticity_in_latent:
            kl_loss = torch.stack(kl_loss)
            kl_loss = kl_loss.sum(dim=1)
        else:
            kl_loss = 0

        return rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, kl_loss

    def compute_loss_split_batches_by_elbo(self, latent_mean, latent_logvar, vae_prev_obs, vae_next_obs, vae_actions,
                                           vae_rewards, vae_tasks, trajectory_lens):

        """
        Loop over the elvo_t terms to compute losses per t.
        Saves some memory if batch sizes are very large,
        or if trajectory lengths are different, or if we decode only the past.
        """

        rew_reconstruction_loss = []
        state_reconstruction_loss = []
        task_reconstruction_loss = []

        assert len(np.unique(trajectory_lens)) == 1
        n_horizon = np.unique(trajectory_lens)[0]
        n_elbos = latent_mean.shape[0]  # includes the prior

        # for each elbo term (including one for the prior)...
        for idx_elbo in range(n_elbos):

            # get the embedding values (size: traj_length+1 * latent_dim; the +1 is for the prior)
            curr_means = latent_mean[idx_elbo]
            curr_logvars = latent_logvar[idx_elbo]

            # take one sample for each task
            if not self.args.disable_stochasticity_in_latent:
                curr_samples = self.encoder._sample_gaussian(curr_means, curr_logvars)
            else:
                curr_samples = torch.cat((latent_mean, latent_logvar))

            # if the size of what we decode is always the same, we can speed up creating the batches
            if not self.args.decode_only_past:

                # expand the latent to match the (x, y) pairs of the decoder
                dec_embedding = curr_samples.unsqueeze(0).expand((n_horizon, *curr_samples.shape))
                dec_embedding_task = curr_samples

                dec_prev_obs = vae_prev_obs
                dec_next_obs = vae_next_obs
                dec_actions = vae_actions
                dec_rewards = vae_rewards

            # otherwise, we unfortunately have to loop!
            # loop through the lengths we are feeding into the encoder for that trajectory (starting with prior)
            # (these are the different ELBO_t terms)
            else:

                # get the index until which we want to decode
                # (i.e. eithe runtil curr timestep or entire trajectory including future)
                if self.args.decode_only_past:
                    dec_from = 0
                    dec_until = idx_elbo
                else:
                    dec_from = 0
                    dec_until = n_horizon

                if dec_from == dec_until:
                    continue

                # (1) ... get the latent sample after feeding in some data (determined by len_encoder) & expand (to number of outputs)
                # num latent samples x embedding size
                dec_embedding = curr_samples.unsqueeze(0).expand(dec_until - dec_from, *curr_samples.shape)
                dec_embedding_task = curr_samples
                # (2) ... get the predictions for the trajectory until the timestep we're interested in
                dec_prev_obs = vae_prev_obs[dec_from:dec_until]
                dec_next_obs = vae_next_obs[dec_from:dec_until]
                dec_actions = vae_actions[dec_from:dec_until]
                dec_rewards = vae_rewards[dec_from:dec_until]

            if self.args.decode_reward:
                # compute reconstruction loss for this trajectory (for each timestep that was encoded, decode everything and sum it up)
                # size: if all trajectories are of same length [num_elbo_terms x num_reconstruction_terms], otherwise it's flattened into one
                rrc = self.compute_rew_reconstruction_loss(dec_embedding, dec_prev_obs, dec_next_obs, dec_actions,
                                                           dec_rewards)
                # sum up the reconstruction terms; average over tasks
                rrc = rrc.sum(dim=0).mean()
                rew_reconstruction_loss.append(rrc)

            if self.args.decode_state:
                src = self.compute_state_reconstruction_loss(dec_embedding, dec_prev_obs, dec_next_obs, dec_actions)
                # sum up the reconstruction terms; average over tasks
                src = src.sum(dim=0).mean()
                state_reconstruction_loss.append(src)

            if self.args.decode_task:
                trc = self.compute_task_reconstruction_loss(dec_embedding_task, vae_tasks)
                # average across tasks
                trc = trc.mean()
                task_reconstruction_loss.append(trc)

        # sum the ELBO_t terms
        if self.args.decode_reward:
            rew_reconstruction_loss = torch.stack(rew_reconstruction_loss)
            rew_reconstruction_loss = rew_reconstruction_loss.sum()
        else:
            rew_reconstruction_loss = 0

        if self.args.decode_state:
            state_reconstruction_loss = torch.stack(state_reconstruction_loss)
            state_reconstruction_loss = state_reconstruction_loss.sum()
        else:
            state_reconstruction_loss = 0

        if self.args.decode_task:
            task_reconstruction_loss = torch.stack(task_reconstruction_loss)
            task_reconstruction_loss = task_reconstruction_loss.sum()
        else:
            task_reconstruction_loss = 0

        if not self.args.disable_stochasticity_in_latent:
            # compute the KL term for each ELBO term of the current trajectory
            kl_loss = self.compute_kl_loss(latent_mean, latent_logvar, None)
            # sum the elbos, average across tasks
            kl_loss = kl_loss.sum(dim=0).mean()
        else:
            kl_loss = 0

        return rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, kl_loss

    def compute_vae_loss(self, update=False):
        """
        Returns the VAE loss
        """

        if not self.rollout_storage.ready_for_update():
            return 0

        if self.args.disable_decoder and self.args.disable_stochasticity_in_latent:
            return 0

        # get a mini-batch
        vae_prev_obs, vae_next_obs, vae_actions, vae_rewards, vae_tasks, \
        len_encoder, trajectory_lens = self.rollout_storage.get_batch(num_rollouts=self.args.vae_batch_num_trajs,
                                                                      num_enc_len=self.args.vae_batch_num_enc_lens)
        # vae_prev_obs will be of size: max trajectory len x num trajectories x dimension of observations
        # len_encoder will be of size:  number of trajectories x data_per_rollout

        # pass through encoder (outputs will be: (max_traj_len+1) x number of rollouts x latent_dim -- includes the prior!)
        _, latent_mean, latent_logvar, _ = self.encoder(actions=vae_actions,
                                                        states=vae_next_obs,
                                                        rewards=vae_rewards,
                                                        hidden_state=None,
                                                        return_prior=True)

        if self.args.split_batches_by_task:
            losses = self.compute_loss_split_batches_by_task(latent_mean, latent_logvar, vae_prev_obs, vae_next_obs,
                                                             vae_actions, vae_rewards, vae_tasks,
                                                             trajectory_lens, len_encoder)
        elif self.args.split_batches_by_elbo:
            losses = self.compute_loss_split_batches_by_elbo(latent_mean, latent_logvar, vae_prev_obs, vae_next_obs,
                                                             vae_actions, vae_rewards, vae_tasks,
                                                             trajectory_lens)
        else:
            losses = self.compute_loss(latent_mean, latent_logvar, vae_prev_obs, vae_next_obs, vae_actions,
                                       vae_rewards, vae_tasks, trajectory_lens)
        rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, kl_loss = losses

        # VAE loss = KL loss + reward reconstruction + state transition reconstruction
        # take average (this is the expectation over p(M))
        loss = (self.args.rew_loss_coeff * rew_reconstruction_loss +
                self.args.state_loss_coeff * state_reconstruction_loss +
                self.args.task_loss_coeff * task_reconstruction_loss +
                self.args.kl_weight * kl_loss).mean()

        # make sure we can compute gradients
        if not self.args.disable_stochasticity_in_latent:
            assert kl_loss.requires_grad
        if self.args.decode_reward:
            assert rew_reconstruction_loss.requires_grad
        if self.args.decode_state:
            assert state_reconstruction_loss.requires_grad
        if self.args.decode_task:
            assert task_reconstruction_loss.requires_grad

        # overall loss
        elbo_loss = loss.mean()

        if update:
            self.optimiser_vae.zero_grad()
            elbo_loss.backward()
            self.optimiser_vae.step()
            # clip gradients
            # nn.utils.clip_grad_norm_(self.encoder.parameters(), self.args.a2c_max_grad_norm)
            # nn.utils.clip_grad_norm_(reward_decoder.parameters(), self.args.max_grad_norm)

        self.log(elbo_loss, rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, kl_loss)

        return elbo_loss

    def log(self, elbo_loss, rew_reconstruction_loss, state_reconstruction_loss, task_reconstruction_loss, kl_loss):

        curr_iter_idx = self.get_iter_idx()
        if curr_iter_idx % self.args.log_interval == 0:

            if self.args.decode_reward:
                self.logger.add('vae_losses/reward_reconstr_err', rew_reconstruction_loss.mean(), curr_iter_idx)
            if self.args.decode_state:
                self.logger.add('vae_losses/state_reconstr_err', state_reconstruction_loss.mean(), curr_iter_idx)
            if self.args.decode_task:
                self.logger.add('vae_losses/task_reconstr_err', task_reconstruction_loss.mean(), curr_iter_idx)

            if not self.args.disable_stochasticity_in_latent:
                self.logger.add('vae_losses/kl', kl_loss.mean(), curr_iter_idx)
            self.logger.add('vae_losses/sum', elbo_loss, curr_iter_idx)
