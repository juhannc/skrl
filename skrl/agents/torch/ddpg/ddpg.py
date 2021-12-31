from typing import Union, Dict

import torch
import torch.nn.functional as F

from ....envs.torch import Wrapper
from ....memories.torch import Memory
from ....models.torch import Model

from .. import Agent


DDPG_DEFAULT_CONFIG = {
    "gradient_steps": 1,            # gradient steps
    "batch_size": 64,               # training batch size
    
    "discount_factor": 0.99,        # discount factor (gamma)
    "polyak": 0.995,                # soft update hyperparameter (tau)
    
    "actor_learning_rate": 1e-3,    # actor learning rate
    "critic_learning_rate": 1e-3,   # critic learning rate

    "random_timesteps": 1000,       # random exploration steps
    "learning_starts": 1000,        # learning starts after this many steps

    "exploration": {
        "noise": None,              # exploration noise
        "initial_scale": 1.0,       # initial scale for the noise
        "final_scale": 1e-3,        # final scale for the noise
        "timesteps": None,          # timesteps for the noise decay
    },

    "device": None,                 # computing device

    "experiment": {
        "base_directory": "",       # base directory for the experiment
        "experiment_name": "",      # experiment name
        "write_interval": 250,      # write interval for the experiment

        "checkpoint_interval": 1000,        # checkpoint interval for the experiment
        "only_checkpoint_policy": True,     # checkpoint only the policy
    }
}


class DDPG(Agent):
    def __init__(self, env: Wrapper, networks: Dict[str, Model], memory: Union[Memory, None] = None, cfg: dict = {}) -> None:
        """Deep Deterministic Policy Gradient (DDPG)

        https://arxiv.org/abs/1509.02971
        
        :param env: RL environment
        :type env: skrl.env.torch.Wrapper
        :param networks: Networks used by the agent
        :type networks: dictionary of skrl.models.torch.Model
        :param memory: Memory to storage the transitions
        :type memory: skrl.memory.torch.Memory or None
        :param cfg: Configuration dictionary
        :type cfg: dict
        """
        DDPG_DEFAULT_CONFIG.update(cfg)
        super().__init__(env=env, networks=networks, memory=memory, cfg=DDPG_DEFAULT_CONFIG)

        # networks
        if not "policy" in self.networks.keys():
            raise KeyError("Policy network not found in networks. Use 'policy' key to define the policy network")
        if not "target_policy" in self.networks.keys():
            raise KeyError("Target policy network not found in networks. Use 'target_policy' key to define the target policy network")
        if not "q" in self.networks.keys() and not "critic" in self.networks.keys():
            raise KeyError("Q-network (critic) not found in networks. Use 'critic' or 'q' keys to define the Q-network (critic)")
        if not "target_q" in self.networks.keys() and not "target_critic" in self.networks.keys():
            raise KeyError("Target Q-network (target critic) not found in networks. Use 'target_critic' or 'target_q' keys to define the target Q-network (target critic)")
        
        self.policy = self.networks["policy"]
        self.target_policy = self.networks["target_policy"]
        self.critic = self.networks.get("critic", self.networks.get("q", None))
        self.target_critic = self.networks.get("target_critic", self.networks.get("target_q", None))

        # checkpoint networks
        self.checkpoint_networks = {"policy": self.policy} if self.only_checkpoint_policy else self.networks
        
        # freeze target networks with respect to optimizers (update via .update_parameters())
        self.target_policy.freeze_parameters(True)
        self.target_critic.freeze_parameters(True)

        # update target networks (hard update)
        self.target_policy.update_parameters(self.policy, polyak=0)
        self.target_critic.update_parameters(self.critic, polyak=0)

        # configuration
        self._gradient_steps = self.cfg["gradient_steps"]
        self._batch_size = self.cfg["batch_size"]
        
        self._discount_factor = self.cfg["discount_factor"]
        self._polyak = self.cfg["polyak"]

        self._actor_learning_rate = self.cfg["actor_learning_rate"]
        self._critic_learning_rate = self.cfg["critic_learning_rate"]
        
        self._random_timesteps = self.cfg["random_timesteps"]
        self._learning_starts = self.cfg["learning_starts"]

        self._exploration_noise = self.cfg["exploration"]["noise"]
        self._exploration_initial_scale = self.cfg["exploration"]["initial_scale"]
        self._exploration_final_scale = self.cfg["exploration"]["final_scale"]
        self._exploration_timesteps = self.cfg["exploration"]["timesteps"]
        
        # set up optimizers
        self.policy_optimizer = torch.optim.Adam(self.policy.parameters(), lr=self._actor_learning_rate)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=self._critic_learning_rate)

        # create tensors in memory
        self.memory.create_tensor(name="states", size=self.env.observation_space, dtype=torch.float32)
        self.memory.create_tensor(name="next_states", size=self.env.observation_space, dtype=torch.float32)
        self.memory.create_tensor(name="actions", size=self.env.action_space, dtype=torch.float32)
        self.memory.create_tensor(name="rewards", size=1, dtype=torch.float32)
        self.memory.create_tensor(name="dones", size=1, dtype=torch.bool)

        self.tensors_names = ["states", "actions", "rewards", "next_states", "dones"]

    def act(self, states: torch.Tensor, inference: bool = False, timestep: Union[int, None] = None, timesteps: Union[int, None] = None) -> torch.Tensor:
        """Process the environments' states to make a decision (actions) using the main policy

        :param states: Environments' states
        :type states: torch.Tensor
        :param inference: Flag to indicate whether the network is making inference
        :type inference: bool
        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int

        :return: Actions
        :rtype: torch.Tensor
        """
        # sample random actions
        if timestep < self._random_timesteps:
            return self.policy.random_act(states)

        # sample deterministic actions
        actions = self.policy.act(states, inference=inference)

        # add exloration noise
        if self._exploration_noise is not None:
            # sample noises
            noises = self._exploration_noise.sample(actions[0].shape)
            
            # define exploration timesteps
            scale = self._exploration_final_scale
            if self._exploration_timesteps is None:
                self._exploration_timesteps = timesteps
            
            # apply exploration noise
            if timestep <= self._exploration_timesteps:
                scale = (1 - timestep / self._exploration_timesteps) * (self._exploration_initial_scale - self._exploration_final_scale) + self._exploration_final_scale
                noises.mul_(scale)

                # modify actions
                actions[0].add_(noises)
                actions[0].clamp_(self.env.action_space.low[0], self.env.action_space.high[0]) # FIXME: use tensor too

                # record noises
                self.tracking_data["Noise / Exploration noise (max)"].append(torch.max(noises).item())
                self.tracking_data["Noise / Exploration noise (min)"].append(torch.min(noises).item())
                self.tracking_data["Noise / Exploration noise (mean)"].append(torch.mean(noises).item())
            
            else:
                # record noises
                self.tracking_data["Noise / Exploration noise (max)"].append(0)
                self.tracking_data["Noise / Exploration noise (min)"].append(0)
                self.tracking_data["Noise / Exploration noise (mean)"].append(0)
        
        return actions

    def record_transition(self, states: torch.Tensor, actions: torch.Tensor, rewards: torch.Tensor, next_states: torch.Tensor, dones: torch.Tensor, timestep: int, timesteps: int) -> None:
        """Record an environment transition in memory
        
        :param states: Observations/states of the environment used to make the decision
        :type states: torch.Tensor
        :param actions: Actions taken by the agent
        :type actions: torch.Tensor
        :param rewards: Instant rewards achieved by the current actions
        :type rewards: torch.Tensor
        :param next_states: Next observations/states of the environment
        :type next_states: torch.Tensor
        :param dones: Signals to indicate that episodes have ended
        :type dones: torch.Tensor
        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        super().record_transition(states, actions, rewards, next_states, dones, timestep, timesteps)
        if self.memory is not None:
            self.memory.add_samples(states=states, actions=actions, rewards=rewards, next_states=next_states, dones=dones)

    def pre_interaction(self, timestep: int, timesteps: int) -> None:
        """Callback called before the interaction with the environment

        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        pass

    def post_interaction(self, timestep: int, timesteps: int) -> None:
        """Callback called after the interaction with the environment

        :param timestep: Current timestep
        :type timestep: int
        :param timesteps: Number of timesteps
        :type timesteps: int
        """
        if timestep >= self._learning_starts:
            self._update(timestep, timesteps)

        # write tracking data and checkpoints
        super().post_interaction(timestep, timesteps)
    
    def _update(self, timestep: int, timesteps: int):
        # gradient steps
        for gradient_step in range(self._gradient_steps):
            
            # sample a batch from memory
            sampled_states, sampled_actions, sampled_rewards, sampled_next_states, sampled_dones = self.memory.sample(self._batch_size, self.tensors_names)

            # compute target values
            with torch.no_grad():
                next_actions, _, _ = self.target_policy.act(states=sampled_next_states)
                
                target_q_values, _, _ = self.target_critic.act(states=sampled_next_states, taken_actions=next_actions)
                target_values = sampled_rewards + self._discount_factor * sampled_dones.logical_not() * target_q_values

            # compute critic loss
            critic_values, _, _ = self.critic.act(states=sampled_states, taken_actions=sampled_actions)
            
            critic_loss = F.mse_loss(critic_values, target_values)
            
            # optimize critic
            self.critic_optimizer.zero_grad()
            critic_loss.backward()
            self.critic_optimizer.step()

            # compute policy (actor) loss
            actions, _, _ = self.policy.act(states=sampled_states)
            critic_values, _, _ = self.critic.act(states=sampled_states, taken_actions=actions)

            policy_loss = -critic_values.mean()

            # optimize policy (actor)
            self.policy_optimizer.zero_grad()
            policy_loss.backward()
            self.policy_optimizer.step()

            # update target networks
            self.target_policy.update_parameters(self.policy, polyak=self._polyak)
            self.target_critic.update_parameters(self.critic, polyak=self._polyak)

            # record data
            self.tracking_data["Loss / Policy loss"].append(policy_loss.item())
            self.tracking_data["Loss / Critic loss"].append(critic_loss.item())

            self.tracking_data["Q-network / Q1 (max)"].append(torch.max(critic_values).item())
            self.tracking_data["Q-network / Q1 (min)"].append(torch.min(critic_values).item())
            self.tracking_data["Q-network / Q1 (mean)"].append(torch.mean(critic_values).item())

            self.tracking_data["Target / Target (max)"].append(torch.max(target_values).item())
            self.tracking_data["Target / Target (min)"].append(torch.min(target_values).item())
            self.tracking_data["Target / Target (mean)"].append(torch.mean(target_values).item())
