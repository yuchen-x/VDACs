import datetime
import pickle
import os
import pprint
import time
import threading
import torch as th
import numpy as np
import random
from types import SimpleNamespace as SN
from utils.logging import Logger
from utils.timehelper import time_left, time_str
from os.path import dirname, abspath

from learners import REGISTRY as le_REGISTRY
from runners import REGISTRY as r_REGISTRY
from controllers import REGISTRY as mac_REGISTRY
from components.episode_buffer import ReplayBuffer
from components.transforms import OneHot


def run(_run, _config, _log):

    # check args sanity
    _config = args_sanity_check(_config, _log)

    args = SN(**_config)
    args.device = "cuda" if args.use_cuda else "cpu"

    # setup loggers
    logger = Logger(_log)

    # _log.info("Experiment Parameters:")
    # experiment_params = pprint.pformat(_config,
    #                                    indent=4,
    #                                    width=1)
    # _log.info("\n\n" + experiment_params + "\n")

    # # configure tensorboard logger
    # unique_token = "{}__{}".format(args.name, datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
    # args.unique_token = unique_token
    # if args.use_tensorboard:
    #     tb_logs_direc = os.path.join(dirname(dirname(abspath(__file__))), "results", "tb_logs")
    #     tb_exp_direc = os.path.join(tb_logs_direc, "{}").format(unique_token)
    #     logger.setup_tb(tb_exp_direc)

    # sacred is on by default
    logger.setup_sacred(_run)

    # Run and train
    run_sequential(args=args, logger=logger)

    # Clean up after finishing
    print("Exiting Main")

    print("Stopping all threads")
    for t in threading.enumerate():
        if t.name != "MainThread":
            print("Thread {} is alive! Is daemon: {}".format(t.name, t.daemon))
            t.join(timeout=1)
            print("Thread joined")

    print("Exiting script")

    # Making sure framework really exits
    os._exit(os.EX_OK)


def evaluate_sequential(args, runner):

    for _ in range(args.test_nepisode):
        runner.run(test_mode=True)

    if args.save_replay:
        runner.save_replay()

    runner.close_env()

def run_sequential(args, logger):

    # create the dirs to save results
    os.makedirs("./performance/" + args.save_dir + "/train", exist_ok=True)
    os.makedirs("./performance/" + args.save_dir + "/test", exist_ok=True)
    os.makedirs("./performance/" + args.save_dir + "/ckpt", exist_ok=True)

    # Init runner so we can get env info
    runner = r_REGISTRY[args.runner](args=args, logger=logger)

    # Set up schemes and groups here
    env_info = runner.get_env_info()
    args.n_agents = env_info["n_agents"]
    args.n_actions = env_info["n_actions"]
    args.state_shape = env_info["state_shape"]

    # Default/Base scheme
    scheme = {
        "state": {"vshape": env_info["state_shape"]},
        "obs": {"vshape": env_info["obs_shape"], "group": "agents"},
        "actions": {"vshape": (1,), "group": "agents", "dtype": th.long},
        "avail_actions": {"vshape": (env_info["n_actions"],), "group": "agents", "dtype": th.int},
        "reward": {"vshape": (1,)},
        "terminated": {"vshape": (1,), "dtype": th.uint8},
    }
    groups = {
        "agents": args.n_agents
    }
    preprocess = {
        "actions": ("actions_onehot", [OneHot(out_dim=args.n_actions)])
    }

    buffer = ReplayBuffer(scheme, groups, args.buffer_size, env_info["episode_limit"] + 1,
                          preprocess=preprocess,
                          device="cpu" if args.buffer_cpu_only else args.device)

    # Setup multiagent controller here
    mac = mac_REGISTRY[args.mac](buffer.scheme, groups, args)

    # Give runner the scheme
    runner.setup(scheme=scheme, groups=groups, preprocess=preprocess, mac=mac)

    # Learner
    learner = le_REGISTRY[args.learner](mac, buffer.scheme, logger, args)

    if args.use_cuda:
        learner.cuda()

    if args.checkpoint_path != "":

        timesteps = []
        timestep_to_load = 0

        if not os.path.isdir(args.checkpoint_path):
            logger.console_logger.info("Checkpoint directiory {} doesn't exist".format(args.checkpoint_path))
            return

        # Go through all files in args.checkpoint_path
        for name in os.listdir(args.checkpoint_path):
            full_name = os.path.join(args.checkpoint_path, name)
            # Check if they are dirs the names of which are numbers
            if os.path.isdir(full_name) and name.isdigit():
                timesteps.append(int(name))

        if args.load_step == 0:
            # choose the max timestep
            timestep_to_load = max(timesteps)
        else:
            # choose the timestep closest to load_step
            timestep_to_load = min(timesteps, key=lambda x: abs(x - args.load_step))

        model_path = os.path.join(args.checkpoint_path, str(timestep_to_load))

        logger.console_logger.info("Loading model from {}".format(model_path))
        learner.load_models(model_path)
        runner.t_env = timestep_to_load

        if args.evaluate or args.save_replay:
            evaluate_sequential(args, runner)
            return

    # start training
    episode = 0
    last_test_T = -args.test_interval - 1
    last_log_T = 0
    model_save_time = 0

    start_time = time.time()
    last_time = start_time

    logger.console_logger.info("Beginning training for {} episodes".format(args.t_max))

    if args.resume:
        episode, test_returns = load_ckpt(args.run_id, learner, mac, args.save_dir)
    else:
        test_returns = []
        episode = 0
    start = time.time()
    while episode <= args.t_max:

        # Run for a whole episode at a time
        episode_batch = runner.run(episode, test_mode=False)
        buffer.insert_episode_batch(episode_batch)

        if buffer.can_sample(args.batch_size):
            episode_sample = buffer.sample(args.batch_size)

            # Truncate batch to only filled timesteps
            max_ep_t = episode_sample.max_t_filled()
            episode_sample = episode_sample[:, :max_ep_t]

            if episode_sample.device != args.device:
                episode_sample.to(args.device)

            learner.train(episode_sample, runner.t_env, episode)

        # Execute test runs once in a while
        n_test_runs = max(1, args.test_nepisode // args.batch_size_run) + 1
        if episode % (args.test_interval - (args.test_interval % args.batch_size_run)) == 0:

            # logger.console_logger.info("t_env: {} / {}".format(runner.t_env, args.t_max))
            # logger.console_logger.info("Estimated time left: {}. Time passed: {}".format(
            #     time_left(last_time, last_test_T, runner.t_env, args.t_max), time_str(time.time() - start_time)))
            # last_time = time.time()

            # last_test_T = runner.t_env
            for _ in range(n_test_runs):
                runner.run(episode, test_mode=True)
            test_returns.append(np.mean(runner.test_returns[0:args.test_nepisode]))
            print(f"[{args.run_id}] [{episode}/{args.t_max}] Evaluate learned policies with averaged return {test_returns[-1]}", flush=True)
            runner.test_returns = []

        if args.save_model and (runner.t_env - model_save_time >= args.save_model_interval or model_save_time == 0):
            model_save_time = runner.t_env
            save_path = os.path.join(args.local_results_path, "models", args.unique_token, str(runner.t_env))
            #"results/models/{}".format(unique_token)
            os.makedirs(save_path, exist_ok=True)
            logger.console_logger.info("Saving models to {}".format(save_path))

            # learner should handle saving/loading -- delegate actor save/load to mac,
            # use appropriate filenames to do critics, optimizer states
            learner.save_models(save_path)

        """
        This block is used to save model whenever the model reaches good test performance
        """
        #if args.save_model and runner.save_model:
        #    save_path = os.path.join(args.local_results_path, "models", args.unique_token, str(runner.t_env))
        #    #"results/models/{}".format(unique_token)
        #    os.makedirs(save_path, exist_ok=True)
        #    logger.console_logger.info("Saving models to {} at the profit of {}".format(save_path, runner.best_performance))

        #    # learner should handle saving/loading -- delegate actor save/load to mac,
        #    # use appropriate filenames to do critics, optimizer states
        #    learner.save_models(save_path)
        #    runner.save_model = False


        episode += args.batch_size_run

        if (time.time() - start) // 3600 >= 23:
            save_ckpt(args.run_id, episode, learner, mac, test_returns, args.save_dir)
            start = time.time()
            break

        # if (runner.t_env - last_log_T) >= args.log_interval:
        #     logger.log_stat("episode", episode, runner.t_env)
        #     logger.print_recent_stats()
        #     last_log_T = runner.t_env

    save_test_data(args.run_id, test_returns, args.save_dir)
    save_train_data(args.run_id, runner.train_returns, args.save_dir)
    # save_ckpt(args.run_id, episode, learner, mac, test_returns, args.save_dir)
    runner.close_env()
    logger.console_logger.info("Finished Training")

def save_train_data(run_idx, data, save_dir):
    with open("./performance/" + save_dir + "/train/train_perform" + str(run_idx) + ".pickle", 'wb') as handle:
        pickle.dump(data, handle)

def save_test_data(run_idx, data, save_dir):
    with open("./performance/" + save_dir + "/test/test_perform" + str(run_idx) + ".pickle", 'wb') as handle:
        pickle.dump(data, handle)

def save_ckpt(run_idx, episode, learner, mac, test_returns, save_dir, max_save=2):

    PATH = "./performance/" + save_dir + "/ckpt/" + str(run_idx) + "_genric_" + "{}.tar"
    for n in list(range(max_save-1, 0, -1)):
        os.system('cp -rf ' + PATH.format(n) + ' ' + PATH.format(n+1) )
    PATH = PATH.format(1)

    th.save({'episode': episode,
             'test_returns': test_returns,
             'random_state': random.getstate(),
             'np_random_state': np.random.get_state(),
             'torch_random_state': th.random.get_rng_state(),
             'cen_critic_net_state_dict': learner.critic.state_dict(),
             'cen_critic_tgt_net_state_dict': learner.target_critic.state_dict(),
             'cen_critic_optimiser_state_dict': learner.critic_optimiser.state_dict(),
             'agent_net_state_dict': mac.agent.state_dict(),
             'agent_net_optimiser_state_dict': learner.agent_optimiser.state_dict(),
             'learner.critic_training_steps': learner.critic_training_steps
             }, PATH)

def load_ckpt(run_idx, learner, mac, save_dir):
    PATH = "./performance/" + save_dir + "/ckpt/" + str(run_idx) + "_genric_" + "1.tar"
    ckpt = th.load(PATH)
    episode = ckpt['episode']
    test_returns = ckpt['test_returns']
    random.setstate(ckpt['random_state'])
    np.random.set_state(ckpt['np_random_state'])
    th.set_rng_state(ckpt['torch_random_state'])
    mac.agent.load_state_dict(ckpt['agent_net_state_dict'])
    learner.critic.load_state_dict(ckpt['cen_critic_net_state_dict'])
    learner.target_critic.load_state_dict(ckpt['cen_critic_tgt_net_state_dict'])
    learner.agent_optimiser.load_state_dict(ckpt['agent_net_optimiser_state_dict'])
    learner.critic_optimiser.load_state_dict(ckpt['cen_critic_optimiser_state_dict'])

    return episode, test_returns


def args_sanity_check(config, _log):

    # set CUDA flags
    # config["use_cuda"] = True # Use cuda whenever possible!
    if config["use_cuda"] and not th.cuda.is_available():
        config["use_cuda"] = False
        _log.warning("CUDA flag use_cuda was switched OFF automatically because no CUDA devices are available!")

    if config["test_nepisode"] < config["batch_size_run"]:
        config["test_nepisode"] = config["batch_size_run"]
    else:
        config["test_nepisode"] = (config["test_nepisode"]//config["batch_size_run"]) * config["batch_size_run"]

    return config
