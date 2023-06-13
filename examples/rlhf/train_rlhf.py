from copy import deepcopy

import torch
from data import get_prompt_dataloader
from env import rollout
from models.actor_critic import init_actor_critic
from models.reward import init_reward_model
from torchrl.data.replay_buffers import (
    LazyTensorStorage,
    SamplerWithoutReplacement,
    TensorDictReplayBuffer,
)
from torchrl.objectives import ClipPPOLoss
from torchrl.objectives.value import GAE
from tqdm import trange
from transformers import GenerationConfig, GPT2Tokenizer
from utils import get_file_logger, load_config, setup

EOS_TOKEN_ID = 50256


def flatten_td(td):
    # our tensordict has shape [B, T] where B = batch_size and T = trajectory length
    # some trajectories may have stopped (reached EOS) before generating T tokens
    # this function truncates and concatenates the trajectories, resulting in a
    # tensordict that has shape [N] where N <= B * T.
    done = td["next", "done"]
    mask = torch.zeros_like(done)
    mask[..., 1:, :] = done[..., :-1, :]  # shift by one
    mask = ~mask.cumsum(-2).bool().squeeze()
    return td[mask]


def create_loss_estimator(
    eval_iters, episode_length, reward_model, batch, ctx, logger=None, ref_model=None
):
    tokenizer = GPT2Tokenizer.from_pretrained("gpt2")
    tokenizer.pad_token = tokenizer.eos_token

    test_rindex = batch.prompt_rindex[0]
    test_prompt_ids = batch.input_ids[:1, :test_rindex]
    test_label_ids = batch.input_ids[:1, test_rindex:]
    generation_config = GenerationConfig(
        pad_token_id=EOS_TOKEN_ID, max_new_tokens=episode_length
    )
    test_prompt = tokenizer.decode(test_prompt_ids[0, :test_rindex].tolist())
    test_label = tokenizer.decode(
        test_label_ids[0, test_label_ids[0] != EOS_TOKEN_ID].tolist()
    )
    _, test_label_reward = reward_model(
        input_ids=batch.input_ids[:1], attention_mask=batch.attention_mask[:1]
    )

    @torch.no_grad()
    def estimate_loss(model, dataloader):
        rewards = torch.zeros(eval_iters)
        for k in range(eval_iters):
            batch = next(dataloader)
            td = rollout(batch, model, ref_model, reward_model)
            rewards[k] = td.get(("next", "reward")).sum(dim=1).mean().item()
        test_reward = rewards.mean()

        if logger:
            response_ids = model.generate(
                input_ids=test_prompt_ids, generation_config=generation_config
            )
            with ctx:
                _, response_reward = reward_model(
                    input_ids=response_ids,
                    attention_mask=(response_ids != EOS_TOKEN_ID).to(torch.int64),
                )
            reward = (response_reward - test_label_reward).item()
            response_ids = response_ids[0, test_rindex:]
            response = tokenizer.decode(
                response_ids[response_ids != tokenizer.eos_token_id].tolist()
            )
            string_to_write = (
                f"Query:\n{test_prompt}\n"
                f"Response:\n{response}\n"
                f"Actual response:\n{test_label}\n"
                f"{reward=:4.4f}, "
                f"{test_reward=:4.4f}\n"
                f"====================================================\n"
            )
            logger.debug(string_to_write)

        return test_reward

    return estimate_loss


def main():
    query_logger = get_file_logger("query_logger", "query_logger.log")
    val_reward_logger = get_file_logger("val_reward_logger", "test_rewards.log")

    config = load_config("config/train_rlhf.yaml")

    data_config = config["data"]
    model_config = config["model"]
    reward_model_config = config["reward_model"]
    train_config = config["train"]
    ppo_config = train_config["ppo"]

    eval_interval = config["io"]["eval_interval"]
    log_interval = config["io"]["log_interval"]
    eval_iters = config["io"]["eval_iters"]

    rlhf_out_dir = model_config["out_dir"]
    transformer_name_or_path = model_config["name_or_path"]
    dropout = model_config["dropout"]

    batch_size = data_config["batch_size"]

    grad_clip = train_config["grad_clip"]
    max_iters = train_config["max_iters"]
    always_save_checkpoint = train_config["always_save_checkpoint"]

    episode_length = ppo_config["episode_length"]
    ppo_batch_size = ppo_config["batch_size"]
    num_epochs = ppo_config["num_epochs"]
    num_rollouts = ppo_config["num_rollouts"]

    device = config["sys"]["device"]
    dtype = config["sys"]["dtype"]
    compile_ = config["sys"]["compile"]

    ctx = setup(device, dtype)

    train_loader = get_prompt_dataloader(data_config, device=device, split="train")
    val_loader = get_prompt_dataloader(data_config, device=device, split="valid")

    actor, critic, critic_head, model = init_actor_critic(
        transformer_name_or_path, dropout, device, compile_
    )
    critic.eval()
    ref_model = deepcopy(model).to("cuda:1")
    ref_model.eval()
    ref_model.requires_grad_(False)
    layers = model.transformer.h
    num_layers = len(layers)
    num_unfrozen = int(0.3 * num_layers)
    for layer in layers[:-num_unfrozen]:
        layer.requires_grad_(False)

    reward_model = init_reward_model(
        reward_model_path=reward_model_config["name_or_path"],
        device=device,
        compile_=compile_,
    )
    reward_model.eval()
    reward_model.requires_grad_(False)

    adv_fn = GAE(value_network=critic, gamma=0.99, lmbda=0.95, average_gae=True)
    loss_fn = ClipPPOLoss(actor, critic_head)

    test_prompt = next(val_loader)
    estimate_loss = create_loss_estimator(
        eval_iters,
        episode_length,
        reward_model,
        test_prompt,
        ctx,
        logger=query_logger,
        ref_model=ref_model,
    )

    optimizer = torch.optim.AdamW(model.parameters(), **train_config["optimizer"])

    rb = TensorDictReplayBuffer(
        storage=LazyTensorStorage(episode_length * num_rollouts),
        batch_size=ppo_batch_size,
        sampler=SamplerWithoutReplacement(),
    )

    best_val_reward = float("-inf")

    for it in trange(1, max_iters + 1):
        rb.empty()
        rollout_rewards = []
        for _ in range(0, num_rollouts, batch_size):
            batch = next(train_loader)
            td = rollout(batch, model, ref_model, reward_model)
            with torch.no_grad(), ctx:
                adv_fn(td)
            # it's possible we didn't fill the replay buffer in the last iteration if
            # generation stopped early, so we empty first before repopulating
            rb.extend(flatten_td(td))
            rollout_rewards.append(td.get(("next", "reward")).mean().cpu().item())
        rollout_reward = torch.tensor(rollout_rewards).mean().cpu().item()

        epoch_losses = []
        for _epoch in range(num_epochs):
            for minibatch in rb:
                optimizer.zero_grad()
                with ctx:
                    loss_vals = loss_fn(minibatch.to(device))
                loss_val = sum(
                    value for key, value in loss_vals.items() if key.startswith("loss")
                )
                loss_val.backward()
                epoch_losses.append(loss_val.detach().cpu())
                torch.nn.utils.clip_grad_norm_(loss_fn.parameters(), grad_clip)
                optimizer.step()

        if it % eval_interval == 0:
            val_reward = estimate_loss(model, val_loader)
            val_reward_logger.debug(val_reward)
            if val_reward > best_val_reward or always_save_checkpoint:
                best_val_reward = val_reward
                if it > 0:
                    print(f"saving checkpoint to {rlhf_out_dir}")
                    model.save_pretrained(rlhf_out_dir)
        elif it % log_interval == 0:
            print(f"{it=}: {rollout_reward=:.4f}")


if __name__ == "__main__":
    main()
