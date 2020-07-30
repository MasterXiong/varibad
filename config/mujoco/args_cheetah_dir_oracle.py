import argparse

import torch

from utils.helpers import boolean_argument


def get_args(rest_args):
    parser = argparse.ArgumentParser()

    # --- GENERAL ---

    parser.add_argument('--num_frames', type=int, default=1e8, help='number of frames to train')
    parser.add_argument('--max_rollouts_per_task', type=int, default=2)
    parser.add_argument('--exp_label', default='oracle', help='label for the experiment')
    parser.add_argument('--env_name', default='HalfCheetahDirOracle-v0', help='environment to train on')

    parser.add_argument('--disable_metalearner', type=boolean_argument, default=True,
                        help='Train a normal policy without the variBAD architecture')

    # --- POLICY ---

    # normalising things
    parser.add_argument('--norm_obs_for_policy', type=boolean_argument, default=True)
    parser.add_argument('--norm_latents_for_policy', type=boolean_argument, default=False)
    parser.add_argument('--norm_rew_for_policy', type=boolean_argument, default=True)
    parser.add_argument('--normalise_actions', type=boolean_argument, default=False, help='normalise policy output')

    # network
    parser.add_argument('--policy_layers', nargs='+', default=[128, 128])
    parser.add_argument('--policy_activation_function', type=str, default='tanh', help='tanh/relu/leaky-relu')
    parser.add_argument('--policy_initialisation', type=str, default='normc', help='normc/orthogonal')
    parser.add_argument('--policy_anneal_lr', type=boolean_argument, default=False)

    # (belief) oracle additions (embedding task/belief state separately before passing to network)
    parser.add_argument('--latent_dim', type=int, default=32)
    parser.add_argument('--state_embedding_size', type=int, default=32)

    # RL algorithm
    parser.add_argument('--policy', type=str, default='a2c', help='choose: a2c, ppo')
    parser.add_argument('--policy_optimiser', type=str, default='rmsprop', help='choose: rmsprop, adam')

    # PPO specific
    parser.add_argument('--ppo_num_epochs', type=int, default=2, help='number of epochs per PPO update')
    parser.add_argument('--ppo_num_minibatch', type=int, default=4, help='number of minibatches to split the data')
    parser.add_argument('--ppo_use_huberloss', type=boolean_argument, default=True, help='use huberloss instead of MSE')
    parser.add_argument('--ppo_use_clipped_value_loss', type=boolean_argument, default=True, help='clip value loss')
    parser.add_argument('--ppo_clip_param', type=float, default=0.05, help='clamp param')

    # A2C specific
    parser.add_argument('--a2c_alpha', type=float, default=0.99, help='RMSprop optimizer alpha (default: 0.99)')

    # other hyperparameters
    parser.add_argument('--lr_policy', type=float, default=7e-4, help='learning rate (default: 7e-4)')
    parser.add_argument('--num_processes', type=int, default=16,
                        help='how many training CPU processes / parallel environments to use (default: 16)')
    parser.add_argument('--policy_num_steps', type=int, default=30,
                        help='number of env steps to do (per process) before updating')
    parser.add_argument('--policy_eps', type=float, default=1e-5, help='optimizer epsilon (1e-8 for ppo, 1e-5 for a2c)')
    parser.add_argument('--policy_init_std', type=float, default=1.0, help='only used for continuous actions')
    parser.add_argument('--policy_value_loss_coef', type=float, default=0.5, help='value loss coefficient')
    parser.add_argument('--policy_entropy_coef', type=float, default=0.1, help='entropy term coefficient')
    parser.add_argument('--policy_gamma', type=float, default=0.95, help='discount factor for rewards')
    parser.add_argument('--policy_use_gae', type=boolean_argument, default=True,
                        help='use generalized advantage estimation')
    parser.add_argument('--policy_tau', type=float, default=0.95, help='gae parameter')
    parser.add_argument('--use_proper_time_limits', type=boolean_argument, default=False,
                        help='treat timeout and death differently (important in mujoco)')
    parser.add_argument('--policy_max_grad_norm', type=float, default=0.5, help='max norm of gradients')

    # --- OTHERS ---

    # logging, saving, evaluation
    parser.add_argument('--log_interval', type=int, default=500, help='log interval, one log per n updates')
    parser.add_argument('--save_interval', type=int, default=1000, help='save interval, one save per n updates')
    parser.add_argument('--eval_interval', type=int, default=500, help='eval interval, one eval per n updates')
    parser.add_argument('--vis_interval', type=int, default=500, help='visualisation interval, one eval per n updates')
    parser.add_argument('--results_log_dir', default=None, help='directory to save results (None uses ./logs)')

    # general settings
    parser.add_argument('--seed',  nargs='+', type=int, default=[73])
    parser.add_argument('--deterministic_execution', type=boolean_argument, default=False,
                        help='Make code fully deterministic. Expects 1 process and uses deterministic CUDNN')

    return parser.parse_args(rest_args)