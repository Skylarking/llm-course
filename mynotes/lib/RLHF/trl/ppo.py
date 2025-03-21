# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/02-ppo.ipynb (unless otherwise specified).

__all__ = ['AdaptiveKLController', 'FixedKLController', 'PPOTrainer']

# Cell
import numpy as np
import torch.nn.functional as F
from torch.optim import Adam
import torch
import collections
import time
import random

from transformers import DataCollatorForLanguageModeling

from .core import (logprobs_from_logits,
                      whiten,
                      clip_by_value,
                      entropy_from_logits,
                      flatten_dict,
                      average_torch_dicts,
                      stats_to_np,
                      stack_dicts,
                      add_suffix,
                      WANDB_PADDING)

# Cell

class AdaptiveKLController:
    """
    Adaptive KL controller described in the paper:
    https://arxiv.org/pdf/1909.08593.pdf
    """
    def __init__(self, init_kl_coef, target, horizon):
        self.value = init_kl_coef
        self.target = target
        self.horizon = horizon

    def update(self, current, n_steps):
        target = self.target
        proportional_error = np.clip(current / target - 1, -0.2, 0.2)
        mult = 1 + proportional_error * n_steps / self.horizon
        self.value *= mult

# Cell

class FixedKLController:
    """Fixed KL controller."""
    def __init__(self, kl_coef):
        self.value = kl_coef

    def update(self, current, n_steps):
        pass

# Cell

class PPOTrainer:
    """
    The PPO_trainer uses Proximal Policy Optimization to optimise language models.
    """

    default_params = {
        "lr": 1.41e-5,
        "adap_kl_ctrl": True,
        "init_kl_coef":0.2,
        "target": 6,
        "horizon":10000,
        "gamma":1,
        "lam":0.95,
        "cliprange": .2,
        "cliprange_value":.2,
        "vf_coef":.1,
        "batch_size": 256,
        "forward_batch_size": 16,
        "ppo_epochs": 4,
    }

    def __init__(self, model, ref_model, tokenizer, **ppo_params):
        """
        Initialize PPOTrainer.

        Args:
            model (torch.model): Hugging Face transformer GPT2 model with value head
            ref_model (torch.model): Hugging Face transformer GPT2 refrence model used for KL penalty
            tokenizer (tokenizer): Hugging Face tokenizer
            ppo_params (dict or None): PPO parameters for training. Can include following keys:
                'lr' (float): Adam learning rate, default: 1.41e-5
                'batch_size' (int): Number of samples per optimisation step, default: 256
                'forward_batch_size' (int): Number of samples forward passed through model at a time, default: 16
                'ppo_epochs' (int): Number of optimisation epochs per batch of samples, default: 4
                'gamma' (float)): Gamma parameter for advantage calculation, default: 1.
                'lam' (float): Lambda parameter for advantage calcualation, default: 0.95
                'cliprange_value' (float): Range for clipping values in loss calculation, default: 0.2
                'cliprange' (float): Range for clipping in PPO policy gradient loss, default: 0.2
                'vf_coef' (float): Scaling factor for value loss, default: 0.1
                'adap_kl_ctrl' (bool): Use adaptive KL control, otherwise linear, default: True
                'init_kl_coef' (float): Initial KL penalty coefficient (used for adaptive and linear control), default: 0.2
                'target' (float): Target KL value for adaptive KL control, default: 6.0
                'horizon' (float): Horizon for adaptive KL control, default: 10000

        """
        self.ppo_params = self.default_params
        self.ppo_params.update(ppo_params)

        self.ref_model = ref_model
        self.model = model
        self.tokenizer = tokenizer
        self.data_collator = DataCollatorForLanguageModeling(tokenizer, mlm=False)

        self.optimizer = Adam(model.parameters(), lr=self.ppo_params['lr'])

        if self.ppo_params['adap_kl_ctrl']:
            self.kl_ctl = AdaptiveKLController(self.ppo_params['init_kl_coef'],
                                               self.ppo_params['target'],
                                               self.ppo_params['horizon'])
        else:
            self.kl_ctl = FixedKLController(self.ppo_params['init_kl_coef'])


    def step(self, queries, responses, scores):
        """
        Run a PPO optimisation step.

        args:
            queries (List): List of tensors containing the encoded queries, shape [query_length]
            responses (List): List of tensors containing the encoded responses, shape [response_length]
            scores (List): tensor containing the scores, shape [batch_size]

        returns:
            train_stats (dict): a summary of the training statistics
        """

        bs = self.ppo_params['batch_size']
        assert bs == len(queries), f"Batch size ({bs}) does not match number of examples ({len(queries)})"

        timing = dict()
        t0 = time.time()

        response_lengths = [len(r) for r in responses]

        t = time.time()
        logprobs, ref_logprobs, values = self.batched_forward_pass(queries, responses)          # 拿到模型生成的tokens的log_prob、token_value
        timing['time/ppo/forward_pass'] = time.time()-t

        t = time.time()
        rewards, non_score_reward = self.compute_rewards(scores, logprobs, ref_logprobs)        # 计算discount reward
        timing['time/ppo/compute_rewards'] = time.time()-t

        t = time.time()
        all_stats = []
        idxs = list(range(bs))
        for _ in range(self.ppo_params['ppo_epochs']):
            random.shuffle(idxs)
            for i in range(bs):
                idx = idxs[i]
                train_stats = self.train_minibatch(logprobs[idx].unsqueeze(0), values[idx].unsqueeze(0),
                                                   rewards[idx].unsqueeze(0), queries[idx].unsqueeze(0),
                                                   responses[idx].unsqueeze(0),
                                                   torch.cat([queries[idx],responses[idx]]).unsqueeze(0))
                all_stats.append(train_stats)
        timing['time/ppo/optimize_step'] = time.time()-t

        t = time.time()
        train_stats = stack_dicts(all_stats)

        # reshape advantages/ratios such that they are not averaged.
        train_stats['policy/advantages'] = torch.flatten(train_stats['policy/advantages']).unsqueeze(0)
        train_stats['policy/advantages'] = torch.nan_to_num(train_stats['policy/advantages'], WANDB_PADDING)
        train_stats['policy/ratio'] = torch.flatten(train_stats['policy/ratio']).unsqueeze(0)

        stats = self.record_step_stats(scores=scores, logprobs=logprobs, ref_logprobs=ref_logprobs,
                                       non_score_reward=non_score_reward, train_stats=train_stats,
                                       kl_coef=self.kl_ctl.value)
        stats = stats_to_np(stats)
        timing['time/ppo/calc_stats'] = time.time()-t

        self.kl_ctl.update(stats['objective/kl'], self.ppo_params['batch_size'])

        timing['time/ppo/total'] = time.time()-t0
        stats.update(timing)
        return stats

    def batched_forward_pass(self, queries, responses):
        """Calculate model outputs in multiple batches."""
        bs = self.ppo_params['batch_size']
        fbs = self.ppo_params['forward_batch_size']
        all_logprobs = []
        all_ref_logprobs = []
        all_values = []

        for i in range(int(bs/fbs)):
            query_batch = queries[i*fbs:(i+1)*fbs]
            response_batch = responses[i*fbs:(i+1)*fbs]
            input_ids = self.data_collator([torch.cat([q, r]) for q, r in zip(query_batch, response_batch)])["input_ids"]
            with torch.no_grad():
                logits, _, v = self.model(input_ids)                                    # logits -> (batch, max_output_len, vocab_size); v -> (batch, max_ouput_len)
                ref_logits, _, _ = self.ref_model(input_ids)
            logprobs = logprobs_from_logits(logits[:,:-1,:], input_ids[:,1:])           # (batch, seq_len - 1)
            ref_logprobs = logprobs_from_logits(ref_logits[:,:-1,:], input_ids[:,1:])   # (batch, seq_len - 1)
            for j in range(fbs):
                start = len(query_batch[j]) - 1                                         # 拿到模型生成部分的信息（去掉prompt部分的信息）
                end = start + len(response_batch[j])
                all_values.append(v[j, start:end])                                      # 生成的tokens的value
                all_logprobs.append(logprobs[j, start:end])                             # 生成的tokens的概率
                all_ref_logprobs.append(ref_logprobs[j, start:end])                     # ref model生成的tokens的概率
        return all_logprobs, all_ref_logprobs, all_values

    def train_minibatch(self, logprobs, values, rewards, query, response, model_input):
        """Train one PPO minibatch"""
        loss_p, loss_v, train_stats  = self.loss(logprobs, values, rewards, query, response, model_input)
        loss = loss_p + loss_v
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        return train_stats

    def compute_rewards(self, scores, logprobs, ref_logprobs):
        """Compute per token rewards from scores and KL-penalty."""
        rewards, non_score_rewards = [], []
        for score, logprob, ref_logprob in zip(scores, logprobs, ref_logprobs):
            kl = logprob - ref_logprob                                                 # (seq_len, )
            non_score_reward = -self.kl_ctl.value * kl                                 # (seq_len, )
            non_score_rewards.append(non_score_reward)
            reward = non_score_reward.clone()                                          # 前面每一个token的reward都来自KL惩罚
            reward[-1] += score                                                        # 在最后一位加上人工给的reward
            rewards.append(reward)
        return rewards, non_score_rewards                                              # (batch, seq_len)

    def loss(self, old_logprobs, values, rewards, query, response, model_input):
        """Calculate policy and value losses."""
        lastgaelam = 0
        advantages_reversed = []
        gen_len = response.shape[1]

        for t in reversed(range(gen_len)):
            nextvalues = values[:, t + 1] if t < gen_len - 1 else 0.0
            delta = rewards[:, t] + self.ppo_params['gamma'] * nextvalues - values[:, t]
            lastgaelam = delta + self.ppo_params['gamma'] * self.ppo_params['lam'] * lastgaelam
            advantages_reversed.append(lastgaelam)
        advantages = torch.stack(advantages_reversed[::-1]).transpose(0, 1)

        returns = advantages + values                                               # (batch, generated_seq_len)
        advantages = whiten(advantages)
        advantages = advantages.detach()

        logits, _, vpred = self.model(model_input)                                  # logits -> (batch, all_seq_len, vocab_size); vpred -> (batch, all_seq_len)
        logprob = logprobs_from_logits(logits[:,:-1,:], model_input[:, 1:])

        #only the generation part of the values/logprobs is needed
        logprob, vpred = logprob[:, -gen_len:], vpred[:,-gen_len-1:-1]              # logprob -> (batch, generated_seq_len); vpred -> (batch, generated_seq_len)

        vpredclipped = clip_by_value(vpred,
                                     values - self.ppo_params["cliprange_value"],
                                     values + self.ppo_params["cliprange_value"])

        vf_losses1 = (vpred - returns)**2                                           # value loss = v - (r + gamma * n_next)
        vf_losses2 = (vpredclipped - returns)**2                                    # value loss clipped
        vf_loss = .5 * torch.mean(torch.max(vf_losses1, vf_losses2))
        vf_clipfrac =  torch.mean(torch.gt(vf_losses2, vf_losses1).double())

        ratio = torch.exp(logprob - old_logprobs)
        pg_losses = -advantages * ratio                                             # importance sampling
        pg_losses2 = -advantages * torch.clamp(ratio,
                                               1.0 - self.ppo_params['cliprange'],
                                               1.0 + self.ppo_params['cliprange'])

        pg_loss = torch.mean(torch.max(pg_losses, pg_losses2))
        pg_clipfrac = torch.mean(torch.gt(pg_losses2, pg_losses).double())

        loss = pg_loss + self.ppo_params['vf_coef'] * vf_loss

        entropy = torch.mean(entropy_from_logits(logits))
        approxkl = .5 * torch.mean((logprob - old_logprobs)**2)
        policykl = torch.mean(logprob - old_logprobs)
        return_mean, return_var = torch.mean(returns), torch.var(returns)
        value_mean, value_var = torch.mean(values), torch.var(values)

        stats = dict(
            loss=dict(policy=pg_loss, value=vf_loss, total=loss),
            policy=dict(entropy=entropy, approxkl=approxkl,policykl=policykl, clipfrac=pg_clipfrac,
                        advantages=advantages, advantages_mean=torch.mean(advantages), ratio=ratio),
            returns=dict(mean=return_mean, var=return_var),
            val=dict(vpred=torch.mean(vpred), error=torch.mean((vpred - returns) ** 2),
                     clipfrac=vf_clipfrac, mean=value_mean, var=value_var),
        )
        return pg_loss, self.ppo_params['vf_coef'] * vf_loss, flatten_dict(stats)


    def record_step_stats(self, kl_coef, **data):
        """Record training step statistics."""
        kl_list = [logprobs-ref_logprobs for logprobs, ref_logprobs in zip(data['logprobs'], data['ref_logprobs'])]
        mean_kl = torch.mean(torch.stack([torch.sum(kl) for kl in kl_list]))
        mean_entropy = torch.mean(torch.stack([torch.sum(-log_probs) for log_probs in data['logprobs']]))
        mean_non_score_reward =torch.mean(torch.stack([torch.sum(non_score_reward) for non_score_reward in data['non_score_reward']]))
        stats = {
            'objective/kl': mean_kl,
            'objective/kl_dist': kl_list,
            'objective/logprobs': data['logprobs'],
            'objective/ref_logprobs': data['ref_logprobs'],
            'objective/kl_coef': kl_coef,
            'objective/entropy': mean_entropy,
            'ppo/mean_non_score_reward': mean_non_score_reward,
        }

        for k, v in data['train_stats'].items():
            stats[f'ppo/{k}'] = torch.mean(v, axis=0)
        stats['ppo/val/var_explained'] = 1 - stats['ppo/val/error'] / stats['ppo/returns/var']
        return stats