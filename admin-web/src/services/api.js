import axios from 'axios';

const client = axios.create({
  baseURL: '/api',
  timeout: 20000,
});

const SYSTEM_USER_KEY = 'system_user';
const AUTH_TOKEN_KEY = 'auth_token';
const LONG_TASK_TIMEOUT_MS = 90 * 60 * 1000;

export const getSystemUser = () => localStorage.getItem(SYSTEM_USER_KEY) || 'admin';
export const setSystemUser = (user) => localStorage.setItem(SYSTEM_USER_KEY, user || 'admin');
export const getAuthToken = () => localStorage.getItem(AUTH_TOKEN_KEY) || '';
export const setAuthToken = (token) => {
  if (token) {
    localStorage.setItem(AUTH_TOKEN_KEY, token);
  } else {
    localStorage.removeItem(AUTH_TOKEN_KEY);
  }
};

client.interceptors.request.use((config) => {
  const user = getSystemUser();
  const token = getAuthToken();
  config.headers = config.headers || {};
  config.headers['X-System-User'] = user;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

export const listTenants = () => client.get('/tenants').then((r) => r.data);

export const getSlices = (tenantId, params) =>
  client.get(`/${tenantId}/slices`, { params }).then((r) => r.data);

export const exportSlicesExcel = (tenantId, params) =>
  client.get(`/${tenantId}/slices/export`, { params, responseType: 'blob' }).then((r) => r.data);

export const getSlicePathTree = (tenantId, params) =>
  client.get(`/${tenantId}/slices/path-tree`, { params }).then((r) => r.data);

export const getSlicePathSummary = (tenantId, params) =>
  client.get(`/${tenantId}/slices/path-summary`, { params }).then((r) => r.data);

export const getSliceImageUrl = (tenantId, imagePath, materialVersionId) => {
  const params = new URLSearchParams();
  params.set('path', imagePath || '');
  if (materialVersionId) params.set('material_version_id', materialVersionId);
  return `/api/${encodeURIComponent(tenantId)}/slices/image?${params.toString()}`;
};

export const fetchSliceImageBlob = (tenantId, imagePath, materialVersionId) =>
  client.get(`/${tenantId}/slices/image`, {
    params: { path: imagePath || '', material_version_id: materialVersionId || undefined },
    responseType: 'blob',
    timeout: 60000,
  }).then((r) => ({
    blob: r.data,
    contentType: String(r.headers?.['content-type'] || ''),
  }));

export const batchReviewSlices = (tenantId, payload) =>
  client.post(`/${tenantId}/slices/review/batch`, payload).then((r) => r.data);

export const updateSliceContent = (tenantId, sliceId, payload) =>
  client.post(`/${tenantId}/slices/${encodeURIComponent(sliceId)}/update`, payload).then((r) => r.data);

export const updateSliceImageAnalysis = (tenantId, sliceId, payload) =>
  client.post(`/${tenantId}/slices/${encodeURIComponent(sliceId)}/images/update`, payload).then((r) => r.data);

export const addSlice = (tenantId, payload) =>
  client.post(`/${tenantId}/slices/add`, payload).then((r) => r.data);

export const reorderSlices = (tenantId, payload) =>
  client.post(`/${tenantId}/slices/order`, payload).then((r) => r.data);

export const mergeSlices = (tenantId, payload) =>
  client.post(`/${tenantId}/slices/merge`, payload).then((r) => r.data);

export const getMappings = (tenantId, params) =>
  client.get(`/${tenantId}/mappings`, { params }).then((r) => r.data);

export const batchReviewMappings = (tenantId, payload) =>
  client.post(`/${tenantId}/mappings/review/batch`, payload).then((r) => r.data);

export const getTenantStats = (tenantId) =>
  client.get(`/${tenantId}/stats`).then((r) => r.data);

export const getQaOverview = (tenantId, params) =>
  client.get(`/${tenantId}/qa/overview`, { params }).then((r) => r.data);

export const listQaRuns = (tenantId, params) =>
  client.get(`/${tenantId}/qa/runs`, { params }).then((r) => r.data);

export const getQaRunDetail = (tenantId, runId) =>
  client.get(`/${tenantId}/qa/runs/${encodeURIComponent(runId)}`).then((r) => r.data);

/** Run offline Judge on this run (optional question_ids for selected questions). */
export const runQaJudge = (tenantId, runId, body = {}) =>
  client.post(`/${tenantId}/qa/runs/${encodeURIComponent(runId)}/run-judge`, body, { timeout: 300000 }).then((r) => r.data);

export const createJudgeTask = (tenantId, payload) =>
  client.post(`/${tenantId}/judge/tasks`, payload).then((r) => r.data);

export const listJudgeTasks = (tenantId, params) =>
  client.get(`/${tenantId}/judge/tasks`, { params }).then((r) => r.data);

export const getJudgeTask = (tenantId, taskId) =>
  client.get(`/${tenantId}/judge/tasks/${encodeURIComponent(taskId)}`).then((r) => r.data);

export const cancelJudgeTask = (tenantId, taskId) =>
  client.post(`/${tenantId}/judge/tasks/${encodeURIComponent(taskId)}/cancel`).then((r) => r.data);

export const listQaLlmCalls = (tenantId, params) =>
  client.get(`/${tenantId}/qa/llm-calls`, { params }).then((r) => r.data);

export const getQaTrends = (tenantId, params) =>
  client.get(`/${tenantId}/qa/trends`, { params }).then((r) => r.data);

export const getQaDrift = (tenantId, params) =>
  client.get(`/${tenantId}/qa/drift`, { params }).then((r) => r.data);

export const getQaThresholds = (tenantId) =>
  client.get(`/${tenantId}/qa/thresholds`).then((r) => r.data);

export const updateQaThresholds = (tenantId, payload) =>
  client.put(`/${tenantId}/qa/thresholds`, payload).then((r) => r.data);

export const getQaConfig = (tenantId) =>
  client.get(`/${tenantId}/qa/config`).then((r) => r.data);

export const updateQaConfig = (tenantId, payload) =>
  client.put(`/${tenantId}/qa/config`, payload).then((r) => r.data);

export const getQaReleases = (tenantId) =>
  client.get(`/${tenantId}/qa/releases`).then((r) => r.data);

export const createQaRelease = (tenantId, payload) =>
  client.post(`/${tenantId}/qa/releases`, payload).then((r) => r.data);

export const getQaPricing = (tenantId) =>
  client.get(`/${tenantId}/qa/pricing`).then((r) => r.data);

export const updateQaPricing = (tenantId, payload) =>
  client.put(`/${tenantId}/qa/pricing`, payload).then((r) => r.data);

export const listQaAlerts = (tenantId, params) =>
  client.get(`/${tenantId}/qa/alerts`, { params }).then((r) => r.data);

export const updateQaAlertStatus = (tenantId, alertId, payload) =>
  client.put(`/${tenantId}/qa/alerts/${encodeURIComponent(alertId)}/status`, payload).then((r) => r.data);

export const getQaReleaseReport = (tenantId, params) =>
  client.get(`/${tenantId}/qa/release-report`, { params }).then((r) => r.data);

export const getQaOpsWeekly = (tenantId, params) =>
  client.get(`/${tenantId}/qa/ops-weekly`, { params }).then((r) => r.data);

export const listMaterials = (tenantId) =>
  client.get(`/${tenantId}/materials`).then((r) => r.data);

export const getMaterialMappingJob = (tenantId, materialVersionId) =>
  client.get(`/${tenantId}/materials/${encodeURIComponent(materialVersionId)}/mapping-job`).then((r) => r.data);

export const setMaterialEffective = (tenantId, materialVersionId) =>
  client.post(`/${tenantId}/materials/effective`, { material_version_id: materialVersionId }).then((r) => r.data);

export const archiveMaterial = (tenantId, materialVersionId) =>
  client.post(`/${tenantId}/materials/${encodeURIComponent(materialVersionId)}/archive`).then((r) => r.data);

export const deleteMaterial = (tenantId, materialVersionId, force = false) =>
  client.delete(`/${tenantId}/materials/${encodeURIComponent(materialVersionId)}`, { params: { force: force ? 1 : 0 } }).then((r) => r.data);

export const resliceMaterial = (tenantId, materialVersionId, options = {}) =>
  client.post(
    `/${tenantId}/materials/${encodeURIComponent(materialVersionId)}/reslice`,
    {},
    { timeout: options.timeout || LONG_TASK_TIMEOUT_MS }
  ).then((r) => r.data);

export const remapMaterial = (tenantId, materialVersionId, options = {}) =>
  client.post(
    `/${tenantId}/materials/${encodeURIComponent(materialVersionId)}/remap`,
    {},
    { timeout: options.timeout || LONG_TASK_TIMEOUT_MS }
  ).then((r) => r.data);

export const uploadMaterial = (tenantId, payload, options = {}) => {
  const form = new FormData();
  if (payload.file) form.append('file', payload.file);
  if (payload.text) form.append('text', payload.text);
  return client.post(`/${tenantId}/materials/upload`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: options.timeout || LONG_TASK_TIMEOUT_MS,
    onUploadProgress: options.onUploadProgress,
  }).then((r) => r.data);
};

export const uploadReferenceAndMap = (tenantId, materialVersionId, file, options = {}) => {
  const form = new FormData();
  if (file) form.append('file', file);
  return client.post(`/${tenantId}/materials/${encodeURIComponent(materialVersionId)}/reference/upload`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: options.timeout || LONG_TASK_TIMEOUT_MS,
    onUploadProgress: options.onUploadProgress,
  }).then((r) => r.data);
};

export const generateQuestions = (tenantId, payload) =>
  client.post(`/${tenantId}/generate`, payload).then((r) => r.data);

export const createGenerateTask = (tenantId, payload) =>
  client.post(`/${tenantId}/generate/tasks`, payload).then((r) => r.data);

export const listGenerateTasks = (tenantId, params) =>
  client.get(`/${tenantId}/generate/tasks`, { params }).then((r) => r.data);

export const getGenerateTask = (tenantId, taskId) =>
  client.get(`/${tenantId}/generate/tasks/${encodeURIComponent(taskId)}`).then((r) => r.data);

/** Request cancel of a running or pending task. */
export const cancelGenerateTask = (tenantId, taskId) =>
  client.post(`/${tenantId}/generate/tasks/${encodeURIComponent(taskId)}/cancel`).then((r) => r.data);

/** Resume an incomplete generate task in-place (same task_id). */
export const resumeGenerateTask = (tenantId, taskId, payload = {}) =>
  client.post(`/${tenantId}/generate/tasks/${encodeURIComponent(taskId)}/resume`, payload).then((r) => r.data);

/** Toggle task auto-bank policy and sync current passed questions. */
export const updateGenerateTaskBankPolicy = (tenantId, taskId, payload) =>
  client.post(`/${tenantId}/generate/tasks/${encodeURIComponent(taskId)}/bank-policy`, payload).then((r) => r.data);

export const listGenerateTemplates = (tenantId) =>
  client.get(`/${tenantId}/generate/templates`).then((r) => r.data);

export const createGenerateTemplate = (tenantId, payload) =>
  client.post(`/${tenantId}/generate/templates`, payload).then((r) => r.data);

export const updateGenerateTemplate = (tenantId, templateId, payload) =>
  client.put(`/${tenantId}/generate/templates/${encodeURIComponent(templateId)}`, payload).then((r) => r.data);

export const deleteGenerateTemplate = (tenantId, templateId) =>
  client.delete(`/${tenantId}/generate/templates/${encodeURIComponent(templateId)}`).then((r) => r.data);

const parseSseChunk = (raw) => {
  const lines = String(raw || '').split('\n');
  let eventName = 'message';
  const dataLines = [];
  for (const line of lines) {
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim() || 'message';
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (!dataLines.length) return null;
  const dataText = dataLines.join('\n');
  let payload = dataText;
  try {
    payload = JSON.parse(dataText);
  } catch (_e) {
    // keep raw text payload
  }
  return { event: eventName, data: payload };
};

export const generateQuestionsStream = async (tenantId, payload, handlers = {}) => {
  const token = getAuthToken();
  const systemUser = getSystemUser();
  const streamUrl = `/api/${encodeURIComponent(tenantId)}/generate/stream`;
  const res = await fetch(streamUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-System-User': systemUser || 'admin',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) {
    // Backward compatibility: old backend versions only expose /generate.
    if (res.status === 404) {
      const fallback = await client.post(`/${tenantId}/generate`, payload, { timeout: LONG_TASK_TIMEOUT_MS }).then((r) => r.data);
      if (typeof handlers.onEvent === 'function') handlers.onEvent('done', fallback);
      return fallback;
    }
    let errMsg = `请求失败 (${res.status})`;
    try {
      const bodyJson = await res.json();
      errMsg = bodyJson?.error?.message || errMsg;
    } catch (_e) {
      // ignore
    }
    throw new Error(errMsg);
  }
  if (!res.body) {
    throw new Error('浏览器不支持流式响应');
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let donePayload = null;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx = buffer.indexOf('\n\n');
    while (idx >= 0) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const evt = parseSseChunk(raw);
      if (evt) {
        if (evt.event === 'done') donePayload = evt.data;
        if (typeof handlers.onEvent === 'function') handlers.onEvent(evt.event, evt.data);
      }
      idx = buffer.indexOf('\n\n');
    }
  }
  if (buffer.trim()) {
    const evt = parseSseChunk(buffer);
    if (evt) {
      if (evt.event === 'done') donePayload = evt.data;
      if (typeof handlers.onEvent === 'function') handlers.onEvent(evt.event, evt.data);
    }
  }
  return donePayload;
};

export const listBankQuestions = (tenantId, params) =>
  client.get(`/${tenantId}/bank`, { params }).then((r) => r.data);

export const deleteBankQuestions = (tenantId, payload) =>
  client.post(`/${tenantId}/bank/delete`, payload).then((r) => r.data);

export const addBankQuestions = (tenantId, payload) =>
  client.post(`/${tenantId}/bank/add`, payload).then((r) => r.data);

export const exportBankQuestions = (tenantId, payload) =>
  client.post(`/${tenantId}/bank/export`, payload, { responseType: 'blob' }).then((r) => r.data);

export const listAdminCities = (params) =>
  client.get('/admin/cities', { params }).then((r) => r.data);

export const upsertAdminCity = (payload) =>
  client.post('/admin/cities', payload).then((r) => r.data);

export const updateAdminCityStatus = (tenantId, payload) =>
  client.post(`/admin/cities/${tenantId}/status`, payload).then((r) => r.data);

export const deleteAdminCity = (tenantId, force = false) =>
  client.delete(`/admin/cities/${tenantId}`, { params: { force: force ? 1 : 0 } }).then((r) => r.data);

export const listAdminUsers = (params) =>
  client.get('/admin/users', { params }).then((r) => r.data);

export const upsertAdminUser = (payload) =>
  client.post('/admin/users/upsert', payload).then((r) => r.data);

export const deleteAdminUser = (payload) =>
  client.post('/admin/users/delete', payload).then((r) => r.data);

export const batchBindAdminUsers = (payload) =>
  client.post('/admin/users/batch-bind', payload).then((r) => r.data);

export const getAdminKeyConfig = () =>
  client.get('/admin/key-config').then((r) => r.data);

export const updateAdminKeyConfig = (payload) =>
  client.put('/admin/key-config', payload).then((r) => r.data);
