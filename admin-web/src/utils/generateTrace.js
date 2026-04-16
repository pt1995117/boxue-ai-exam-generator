export const getTraceDisplayIndex = (item, fallback = 0) => {
  const targetIndex = Number(item?.target_index || 0);
  if (targetIndex > 0) return targetIndex;
  const index = Number(item?.index || 0);
  if (index > 0) return index;
  return fallback;
};

export const getTraceItemKey = (item, fallbackIdx = 0) => {
  const traceId = String(item?.trace_id || '').trim();
  if (traceId) return traceId;
  const questionId = String(item?.question_id || '').trim();
  if (questionId) return questionId;
  return [
    String(item?._subtask_id || item?.subtask_id || '').trim(),
    String(item?._subtask_name || item?.subtask_name || '').trim(),
    getTraceDisplayIndex(item, fallbackIdx + 1),
    Number(item?.index || 0),
    Number(item?.slice_id || 0),
    fallbackIdx,
  ].join('_');
};

export const sortTraceRows = (rows) => {
  const list = Array.isArray(rows) ? rows.filter((x) => x && typeof x === 'object') : [];
  return [...list].sort((a, b) => {
    const ta = getTraceDisplayIndex(a, 0);
    const tb = getTraceDisplayIndex(b, 0);
    if (ta !== tb) return ta - tb;
    const ia = Number(a?.index || 0);
    const ib = Number(b?.index || 0);
    if (ia !== ib) return ia - ib;
    return String(a?.trace_id || a?.question_id || '').localeCompare(String(b?.trace_id || b?.question_id || ''));
  });
};

export const hasTraceFinalJson = (row) => {
  const fj = row?.final_json;
  return Boolean(fj && typeof fj === 'object' && !Array.isArray(fj) && Object.keys(fj).length > 0);
};

export const isTraceSaved = (row) => Boolean(row?.saved) || Boolean(row?.saved_with_issues);

export const countTraceSuccess = (rows) => {
  const list = Array.isArray(rows) ? rows : [];
  const passedTargets = new Set();
  list.forEach((row, idx) => {
    if (!row || typeof row !== 'object') return;
    if (!isTraceSaved(row) || !hasTraceFinalJson(row)) return;
    const targetIndex = getTraceDisplayIndex(row, idx + 1);
    if (targetIndex > 0) passedTargets.add(targetIndex);
  });
  return passedTargets.size;
};

export const countTraceAttempts = (rows) => {
  const list = Array.isArray(rows) ? rows : [];
  let count = 0;
  list.forEach((row) => {
    if (!row || typeof row !== 'object') return;
    const hasOutput = hasTraceFinalJson(row) || isTraceSaved(row);
    const hasReject = Array.isArray(row?.steps) && row.steps.some((s) => String(s?.message || '').includes('审核驳回'));
    const hasReason = Boolean(
      row?.critic_result?.reason
      || row?.critic_result?.fix_reason
      || row?.critic_details
      || row?.critic_last_error_content
    );
    if (hasOutput || hasReject || hasReason) count += 1;
  });
  return count;
};

const flattenLiveSubtaskTraceRows = (liveSubtaskTraces) => {
  const flattened = [];
  liveSubtaskTraces.forEach((sub) => {
    const rows = Array.isArray(sub?.process_trace) ? sub.process_trace : [];
    const subTaskId = String(sub?.task_id || '');
    const subTaskName = String(sub?.task_name || '');
    rows.forEach((row, idx) => {
      if (!row || typeof row !== 'object') return;
      flattened.push({
        ...row,
        _subtask_id: subTaskId,
        _subtask_name: subTaskName,
        _subtask_local_index: idx + 1,
      });
    });
  });
  return flattened;
};

const getTraceDedupKey = (row, fallbackIdx = 0) => {
  const traceId = String(row?.trace_id || '').trim();
  if (traceId) return `trace:${traceId}`;
  const questionId = String(row?.question_id || '').trim();
  if (questionId) return `question:${questionId}`;
  return `fallback:${getTraceItemKey(row, fallbackIdx)}`;
};

const preferTraceRow = (prev, next) => {
  if (!prev) return next;
  const prevSaved = Boolean(prev?.saved) || Boolean(prev?.saved_with_issues);
  const nextSaved = Boolean(next?.saved) || Boolean(next?.saved_with_issues);
  if (prevSaved !== nextSaved) return nextSaved ? next : prev;
  const prevHasFinal = hasTraceFinalJson(prev);
  const nextHasFinal = hasTraceFinalJson(next);
  if (prevHasFinal !== nextHasFinal) return nextHasFinal ? next : prev;
  const prevSteps = Array.isArray(prev?.steps) ? prev.steps.length : 0;
  const nextSteps = Array.isArray(next?.steps) ? next.steps.length : 0;
  if (prevSteps !== nextSteps) return nextSteps > prevSteps ? next : prev;
  const prevElapsed = Number(prev?.elapsed_ms || 0);
  const nextElapsed = Number(next?.elapsed_ms || 0);
  if (prevElapsed !== nextElapsed) return nextElapsed > prevElapsed ? next : prev;
  return next;
};

export const mergeTaskTraceForDisplay = (task) => {
  const processTrace = Array.isArray(task?.process_trace) ? task.process_trace.filter((x) => x && typeof x === 'object') : [];
  const liveSubtaskTraces = Array.isArray(task?.live_subtask_traces)
    ? task.live_subtask_traces.filter((x) => x && typeof x === 'object')
    : [];
  const mergedRows = [];
  const deduped = new Map();
  const appendRows = (rows) => {
    rows.forEach((row, idx) => {
      if (!row || typeof row !== 'object') return;
      const key = getTraceDedupKey(row, idx);
      const prev = deduped.get(key);
      deduped.set(key, preferTraceRow(prev, row));
    });
  };
  appendRows(processTrace);
  appendRows(flattenLiveSubtaskTraceRows(liveSubtaskTraces));
  deduped.forEach((row) => mergedRows.push(row));
  return mergedRows.sort((a, b) => {
    const ta = getTraceDisplayIndex(a, 0);
    const tb = getTraceDisplayIndex(b, 0);
    if (ta !== tb) return ta - tb;
    const sa = String(a?._subtask_id || a?.subtask_id || '');
    const sb = String(b?._subtask_id || b?.subtask_id || '');
    if (sa !== sb) return sa.localeCompare(sb);
    const ia = Number(a?.index || 0);
    const ib = Number(b?.index || 0);
    if (ia !== ib) return ia - ib;
    return String(a?.trace_id || a?.question_id || '').localeCompare(String(b?.trace_id || b?.question_id || ''));
  });
};
