# 知识切片到母题自动关联 - 快速开始

## 功能说明

本脚本实现了知识切片到母题的自动关联，采用5种策略确保80%以上的自动关联率：

1. **反向索引**：利用现有映射直接关联
2. **路径匹配**：基于篇/章/节层级匹配
3. **考点匹配**：精确匹配考点字段
4. **TF-IDF语义相似度**：基于内容相似度匹配
5. **LLM重排序**：低置信度时使用大模型判断

## 快速开始

### 1. 确保依赖已安装

```bash
pip install pandas scikit-learn openpyxl openai
```

### 2. 配置API密钥（可选，仅LLM策略需要）

编辑 `填写您的Key.txt`：
```
DEEPSEEK_API_KEY=your_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-reasoner
```

**注意**：即使没有API密钥，前4种策略仍可正常工作，只是不会使用LLM重排序。

### 3. 运行脚本

```bash
python map_knowledge_to_questions.py
```

### 4. 查看结果

- **输出文件**：`knowledge_question_mapping.json`
- **格式**：每个知识切片对应一个条目，包含匹配的母题列表

## 输出示例

```json
{
  "0": {
    "完整路径": "第一篇  行业与贝壳 > 第一章  认识行业 > 第一节  房地产行业 > 一、房地产行业的构成",
    "掌握程度": "了解",
    "matched_questions": [
      {
        "question_index": 0,
        "confidence": 1.0,
        "method": "reverse_index",
        "evidence": {
          "source": "existing_mapping"
        }
      },
      {
        "question_index": 5,
        "confidence": 0.85,
        "method": "exact_kaodian_match",
        "evidence": {
          "kaodian": "房地产行业的构成"
        }
      }
    ],
    "total_matches": 2,
    "methods_used": ["reverse_index", "exact_kaodian_match"]
  }
}
```

## 统计信息解读

运行完成后，脚本会输出：

1. **总体统计**：
   - 总知识切片数
   - 有匹配的切片数及占比
   - 总匹配数
   - 平均每个切片的匹配数

2. **方法统计**：
   - 各策略的使用次数和占比

3. **自动关联率**：
   - 不使用LLM的自动关联比例（目标≥80%）

4. **置信度分布**：
   - 高置信度（≥0.7）
   - 中置信度（0.3-0.7）
   - 低置信度（<0.3）

## 参数调整

如需调整自动关联阈值，编辑 `map_knowledge_to_questions.py` 中的参数：

```python
AUTO_PASS_TARGET = 0.8  # 目标自动关联率
MIN_AUTO_PASS_TFIDF = 0.25  # TF-IDF最低阈值
MIN_AUTO_PASS_COVERAGE = 0.25  # 关键词覆盖率最低阈值
LLM_CONFIDENCE_THRESHOLD = 0.2  # LLM触发阈值
```

## 常见问题

### Q: 为什么有些知识切片没有匹配到母题？

A: 可能原因：
1. 该知识点确实没有对应的母题
2. 语义相似度较低，未达到自动通过阈值
3. 可以尝试降低 `MIN_AUTO_PASS_TFIDF` 和 `MIN_AUTO_PASS_COVERAGE` 阈值

### Q: 如何提高自动关联率？

A: 可以：
1. 降低自动通过阈值（`MIN_AUTO_PASS_TFIDF`、`MIN_AUTO_PASS_COVERAGE`）
2. 增加LLM重排序的使用（降低 `LLM_CONFIDENCE_THRESHOLD`）
3. 确保 `question_knowledge_mapping.json` 存在且完整

### Q: LLM调用失败怎么办？

A: 脚本会继续运行，只是不使用LLM策略。前4种策略仍可正常工作。

### Q: 运行时间需要多久？

A: 取决于：
- 知识切片数量（通常500-1000个）
- 母题数量（通常400-500个）
- 是否使用LLM（LLM会增加时间）

预计时间：
- 不使用LLM：1-3分钟
- 使用LLM：5-15分钟（取决于需要LLM判断的数量）

## 后续使用

生成的 `knowledge_question_mapping.json` 可以用于：

1. **题目生成**：为每个知识切片提供相关母题范例
2. **知识检索**：根据知识切片快速找到相关题目
3. **质量分析**：分析哪些知识点有足够的母题支撑

## 技术支持

如有问题，请查看：
- `知识切片关联方案说明.md` - 详细技术方案
- `map_knowledge_to_questions.py` - 源代码及注释
