/**
 * `process_trace` 中的 `final_json` 多为 writer/fixer 最后一轮输出，只有 `saved===true` 才表示已写入题库。
 *
 * @param {{ saved?: boolean, critic_result?: { passed?: boolean } }} item - 单题 trace 项
 * @returns {{ title: string, tooltip: string, tag: string, tagColor: string }} 卡片标题、说明与状态标签
 */
export function getFinalQuestionPreviewCardMeta(item) {
  const saved = Boolean(item?.saved);
  if (saved) {
    return {
      title: '本题最终内容',
      tag: '已入库',
      tagColor: 'success',
      tooltip:
        '该题已落库。若经过多轮 Fixer，此处与步骤流水里较早的定稿可能不同。',
    };
  }
  const passed =
    item?.critic_result && typeof item.critic_result.passed === 'boolean'
      ? item.critic_result.passed
      : null;
  if (passed === false) {
    return {
      title: '本题最后一轮定稿',
      tag: '未入库',
      tagColor: 'error',
      tooltip:
        '审核未通过或未完成保存。下方仅为当次流程的题目预览，不会作为入库版本写入题库。',
    };
  }
  if (passed === true) {
    return {
      title: '本题定稿',
      tag: '待入库',
      tagColor: 'processing',
      tooltip:
        '审核已通过，但本题可能尚未落库；请以任务摘要「生成结果 / 入库」与下方题目结果表为准。',
    };
  }
  return {
    title: '本题当前内容',
    tag: '预览',
    tagColor: 'default',
    tooltip: '流程未结束或尚无审核结果；非入库版本。',
  };
}
