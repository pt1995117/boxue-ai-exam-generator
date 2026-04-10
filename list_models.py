#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Print configured model endpoints from local key file."""

from runtime_paths import load_primary_key_config


def load_config() -> dict[str, str]:
    return load_primary_key_config()


def main() -> None:
    cfg = load_config()
    print("当前配置：")
    print(f"- OPENAI_BASE_URL: {cfg.get('OPENAI_BASE_URL', 'https://openapi-ait.ke.com')}")
    print(f"- OPENAI_MODEL: {cfg.get('OPENAI_MODEL', 'deepseek-reasoner')}")
    print(f"- DEEPSEEK_BASE_URL: {cfg.get('DEEPSEEK_BASE_URL', 'https://openapi-ait.ke.com')}")
    print(f"- DEEPSEEK_MODEL: {cfg.get('DEEPSEEK_MODEL', 'deepseek-reasoner')}")
    print(f"- ARK_BASE_URL: {cfg.get('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')}")
    print(f"- CRITIC_MODEL: {cfg.get('CRITIC_MODEL', cfg.get('OPENAI_MODEL', 'deepseek-reasoner'))}")
    print(f"- CODE_GEN_MODEL: {cfg.get('CODE_GEN_MODEL', 'doubao-seed-1.8')}")


if __name__ == "__main__":
    main()
