# 10道题离线Judge测试报告

## 汇总

- 总题数：10
- Action 分布：{'PASS': 5, 'FIX_TEXT': 3, 'REGEN_QUESTION': 2}
- Decision 分布：{'PASS': 5, 'NEEDS_MINOR_FIX': 3, 'REJECT': 2}

## 逐题结果

| 题号 | Action | Decision | 置信度 | 风险 | 干扰项分数 | 主要问题 |
|---|---|---|---:|---|---:|---|
| T-001 | PASS | PASS | 0.90 | LOW | 4 | 无 |
| T-002 | FIX_TEXT | NEEDS_MINOR_FIX | 0.90 | MEDIUM | 4 | 人物称谓可能不规范：(先生|女士) |
| T-003 | REGEN_QUESTION | REJECT | 0.90 | LOW | 3 | 选项中出现违禁兜底表述：以上皆是 |
| T-004 | FIX_TEXT | NEEDS_MINOR_FIX | 0.90 | MEDIUM | 4 | 题干城市(上海)与教材城市(北京)可能不一致 |
| T-005 | PASS | PASS | 0.90 | LOW | 4 | 题干存在冗余场景描述：师傅告诉徒弟 |
| T-006 | REGEN_QUESTION | REJECT | 0.90 | LOW | 3 | 应使用全角中文括号（ ）而非半角 () |
| T-007 | PASS | PASS | 0.90 | LOW | 4 | 无 |
| T-008 | PASS | PASS | 0.90 | LOW | 4 | 数值型选项建议按从小到大升序排列 |
| T-009 | PASS | PASS | 0.90 | LOW | 4 | 无 |
| T-010 | FIX_TEXT | NEEDS_MINOR_FIX | 0.90 | MEDIUM | 4 | 普通住宅得房率低于70%，疑似不符合常识 |

## 原始文件

- JSON: `outputs/ten_questions_full_result.json`