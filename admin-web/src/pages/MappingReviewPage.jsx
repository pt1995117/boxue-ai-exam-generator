import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Col, Empty, Form, Input, Modal, Row, Select, Slider, Space, Tag, Tooltip, Tree, Typography, message } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import { batchReviewMappings, fetchSliceImageBlob, getMappings, getMaterialMappingJob, getSliceImageUrl, listMaterials, getSystemUser } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';
import MarkdownWithMermaid from '../components/MarkdownWithMermaid';

const statusColor = { pending: 'default', approved: 'green' };
const statusLabel = { pending: '待审核', approved: '已通过' };
const hasMarkdownTable = (text) => {
  const lines = String(text || '').split('\n').map((x) => x.trim()).filter(Boolean);
  for (let i = 0; i < lines.length - 1; i += 1) {
    const h = lines[i];
    const d = lines[i + 1];
    if (!h.includes('|') || !d.includes('|')) continue;
    const delim = d.replace(/\|/g, '').replace(/[:\-\s]/g, '');
    if (!delim) return true;
  }
  return false;
};

const escapeRegExp = (value) => String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

const SliceImagePreview = React.memo(({ tenantId, imagePath, materialVersionId }) => {
  const [src, setSrc] = useState('');
  useEffect(() => {
    let cancelled = false;
    let objectUrl = '';
    const run = async () => {
      try {
        const { blob, contentType } = await fetchSliceImageBlob(tenantId, imagePath, materialVersionId);
        const ct = String(contentType || '').toLowerCase();
        if (!blob || blob.size <= 0) return;
        if (ct.includes('application/json') || ct.includes('text/html') || ct.includes('text/plain')) return;
        objectUrl = window.URL.createObjectURL(blob);
        if (!cancelled) setSrc(objectUrl);
      } catch (_e) {
        if (!cancelled) setSrc('');
      }
    };
    run();
    return () => {
      cancelled = true;
      if (objectUrl) window.URL.revokeObjectURL(objectUrl);
    };
  }, [tenantId, imagePath, materialVersionId]);
  if (!src) {
    return <Typography.Text type="secondary">图片加载中...</Typography.Text>;
  }
  return (
    <img
      src={src}
      alt={String(imagePath || '').split('/').pop() || 'slice-image'}
      style={{
        display: 'block',
        maxWidth: '100%',
        maxHeight: 220,
        marginTop: 6,
        border: '1px solid #f0f0f0',
        borderRadius: 6,
        objectFit: 'contain',
        background: '#fff',
      }}
    />
  );
});

/**
 * 按切片预览方式：将切片内容中的图片引用转为可点击链接
 * @param {string} text - 切片文本
 * @param {object} row - 映射行（含 images、material_version_id）
 * @param {string} tenantId - 租户ID
 * @param {string} materialVersionId - 教材版本ID
 * @returns {string} 注入链接后的 Markdown 文本
 */
const injectImageLinksToMarkdown = (text, row, tenantId, materialVersionId) => {
  const content = String(text || '');
  if (!content) return '（空）';
  const items = Array.isArray(row?.images) ? row.images : [];
  const map = new Map();
  items.forEach((img) => {
    const path = String(img?.image_path || '').trim();
    if (!path) return;
    const url = getSliceImageUrl(tenantId, path, row.material_version_id || materialVersionId);
    const basename = path.split('/').pop() || '';
    const imageId = String(img?.image_id || '').trim();
    map.set(path, { token: path, url });
    if (basename) map.set(basename, { token: basename, url });
    if (imageId && imageId !== basename) map.set(imageId, { token: imageId, url });
  });
  const targets = Array.from(map.values()).sort((a, b) => (b.token?.length || 0) - (a.token?.length || 0));
  if (!targets.length) return content;
  const lines = content.split('\n');
  let inCodeFence = false;
  const transformed = lines.map((line) => {
    const t = String(line || '');
    if (t.trim().startsWith('```')) {
      inCodeFence = !inCodeFence;
      return t;
    }
    if (inCodeFence) return t;
    let next = t;
    const placeholders = [];
    targets.forEach((target, idx) => {
      const token = String(target.token || '').trim();
      if (!token || !next.includes(token)) return;
      const holder = `__IMG_LINK_${idx}_${token.length}__`;
      next = next.replace(new RegExp(escapeRegExp(token), 'g'), holder);
      placeholders.push({ holder, token, url: target.url });
    });
    placeholders.forEach(({ holder, token, url }) => {
      next = next.replace(new RegExp(escapeRegExp(holder), 'g'), `[${token}](${url})`);
    });
    return next;
  });
  return transformed.join('\n');
};

export default function MappingReviewPage() {
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [rows, setRows] = useState([]);
  const [selectedRowKeys, setSelectedRowKeys] = useState([]);
  const [filters, setFilters] = useState({ status: 'all', keyword: '', meta_conflict: 'all' });
  const [materials, setMaterials] = useState([]);
  const [materialVersionId, setMaterialVersionId] = useState('');
  const [activeMapKey, setActiveMapKey] = useState(null);
  const [previewMapKeys, setPreviewMapKeys] = useState([]);
  const [previewMermaid, setPreviewMermaid] = useState({ open: false, code: '', title: '', zoom: 120 });
  const [editingMapKey, setEditingMapKey] = useState('');
  const [editingTargetId, setEditingTargetId] = useState('');
  const [editingComment, setEditingComment] = useState('');
  const [editingManualStem, setEditingManualStem] = useState('');
  const [editingManualOptions, setEditingManualOptions] = useState('');
  const [editingManualExplanation, setEditingManualExplanation] = useState('');
  const [savingEdit, setSavingEdit] = useState(false);
  const [approvingAll, setApprovingAll] = useState(false);
  const [mappingJob, setMappingJob] = useState(null);
  const systemUser = getSystemUser();

  const extractMermaid = (text) => {
    const raw = String(text || '');
    const match = raw.match(/```mermaid\s*([\s\S]*?)```/);
    return match ? match[1].trim() : '';
  };

  const materialLabel = (m) => {
    const raw = String(m?.file_path || '').split('/').pop() || '';
    const name = raw.replace(/^v\d{8}_\d{6}_/, '') || raw || m?.material_version_id;
    return `${name}${m?.status === 'effective' ? '（当前生效）' : ''}`;
  };

  const toMaterialName = (m) => {
    const raw = String(m?.file_path || '').split('/').pop() || '';
    return raw.replace(/^v\d{8}_\d{6}_/, '') || raw || String(m?.material_version_id || '');
  };

  const refreshMaterials = async (tid, { silent = false } = {}) => {
    if (!tid) return;
    try {
      const res = await listMaterials(tid);
      const items = res.items || [];
      const visibleItems = items.filter((x) => (
        String(x?.status || '') !== 'archived' && String(x?.mapping_status || '') === 'success'
      ));
      setMaterials(visibleItems);
      const effective = visibleItems.find((x) => x.status === 'effective');
      setMaterialVersionId((prev) => {
        const current = String(prev || '');
        if (current && visibleItems.some((m) => String(m?.material_version_id || '') === current)) return current;
        return String((effective || visibleItems[0] || {}).material_version_id || '');
      });

      const running = items.find((x) => String(x?.mapping_status || '') === 'running');
      if (!running) {
        setMappingJob(null);
        return;
      }
      const target = String(running?.material_version_id || '');
      if (!target) {
        setMappingJob(null);
        return;
      }
      const jobRes = await getMaterialMappingJob(tid, target);
      const job = jobRes?.job || {};
      setMappingJob({
        material_version_id: target,
        material_name: toMaterialName(running),
        status: String(job?.status || 'running'),
        progress: Math.max(0, Math.min(100, Number(job?.progress || running?.mapping_progress || 0))),
        message: String(job?.message || running?.mapping_message || ''),
      });
    } catch (e) {
      if (!silent) {
        message.error(e?.response?.data?.error?.message || '加载教材版本失败');
      }
      setMaterials([]);
      setMaterialVersionId('');
      setMappingJob(null);
    }
  };

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);
  const loadData = async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      let page = 1;
      const all = [];
      let total = 0;
      while (true) {
        // eslint-disable-next-line no-await-in-loop
        const res = await getMappings(tenantId, {
          status: filters.status,
          keyword: filters.keyword,
          meta_conflict: filters.meta_conflict,
          material_version_id: materialVersionId || undefined,
          page,
          page_size: 200,
        });
        const chunk = res.items || [];
        all.push(...chunk);
        total = res.total || 0;
        if (all.length >= total || chunk.length === 0) break;
        page += 1;
      }
      setRows(all);
      setSelectedRowKeys([]);
      const firstKey = all[0]?.map_key || null;
      setActiveMapKey(firstKey);
      setPreviewMapKeys(all.map((x) => String(x.map_key)).filter(Boolean));
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '加载映射失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, materialVersionId]);

  useEffect(() => {
    if (!tenantId) return;
    refreshMaterials(tenantId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  useEffect(() => {
    if (!tenantId || !mappingJob || mappingJob.status !== 'running') return undefined;
    const timer = setInterval(() => {
      refreshMaterials(tenantId, { silent: true });
    }, 3000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, mappingJob?.status, mappingJob?.material_version_id]);

  const metrics = useMemo(() => {
    const total = rows.length;
    const pending = rows.filter((x) => x.confirm_status === 'pending').length;
    const approved = rows.filter((x) => x.confirm_status === 'approved').length;
    return { total, pending, approved };
  }, [rows]);

  const splitOptionsText = (text) => String(text || '')
    .split('\n')
    .map((x) => String(x || '').replace(/^[A-Ha-h][\.\、\s]+/, '').trim())
    .filter(Boolean)
    .slice(0, 8);

  const isRowReadyForReview = (row) => {
    if (row?.review_ready === true) return true;
    const stem = String(row?.question_stem || '').trim();
    const explanation = String(row?.question_explanation || '').trim();
    const options = Array.isArray(row?.question_options)
      ? row.question_options.map((x) => String(x || '').trim()).filter(Boolean)
      : [];
    return Boolean(stem && explanation && options.length > 0);
  };

  const onBatchSubmit = async (values) => {
    if (!selectedRowKeys.length) {
      message.warning('请先选择记录');
      return;
    }
    const selectedRows = rows.filter((x) => selectedRowKeys.includes(String(x.map_key || '')));
    const readyRows = selectedRows.filter((x) => isRowReadyForReview(x));
    if (!readyRows.length) {
      message.warning('所选映射未补全母题题干/选项/解析，无法审核');
      return;
    }
    try {
      await batchReviewMappings(tenantId, {
        map_keys: readyRows.map((x) => String(x.map_key)),
        confirm_status: values.confirm_status,
        comment: values.comment || '',
        reviewer: systemUser,
        target_mother_question_id: values.target_mother_question_id || '',
        material_version_id: materialVersionId || undefined,
      });
      message.success(`已更新 ${readyRows.length} 条映射`);
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '批量确认失败');
    }
  };

  const onApproveAllInCurrentTree = async () => {
    const ids = rows.filter((x) => isRowReadyForReview(x)).map((x) => String(x.map_key || '')).filter(Boolean);
    if (!ids.length) {
      message.warning('当前筛选下没有可审核映射（需先补全母题题干/选项/解析）');
      return;
    }
    setApprovingAll(true);
    try {
      const chunkSize = 200;
      for (let i = 0; i < ids.length; i += chunkSize) {
        // eslint-disable-next-line no-await-in-loop
        await batchReviewMappings(tenantId, {
          map_keys: ids.slice(i, i + chunkSize),
          confirm_status: 'approved',
          reviewer: systemUser,
          material_version_id: materialVersionId || undefined,
        });
      }
      message.success(`当前筛选下 ${ids.length} 条映射已全部通过`);
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '全部通过失败');
    } finally {
      setApprovingAll(false);
    }
  };

  const onQuickConfirm = async (mapKey, confirmStatus = 'approved') => {
    if (confirmStatus === 'approved') {
      const row = rows.find((x) => String(x.map_key || '') === String(mapKey));
      if (!row || !isRowReadyForReview(row)) {
        message.warning('请先补全母题题干/选项/解析，再执行通过审核');
        return;
      }
    }
    try {
      await batchReviewMappings(tenantId, {
        map_keys: [mapKey],
        confirm_status: confirmStatus,
        reviewer: systemUser,
        material_version_id: materialVersionId || undefined,
      });
      message.success(`映射 ${mapKey} 已更新`);
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '快捷确认失败');
    }
  };

  const onEditMapping = async (row) => {
    const mapKey = String(row.map_key || '');
    if (!mapKey) return;
    try {
      await onQuickConfirm(mapKey, 'pending');
      setEditingMapKey(mapKey);
      setEditingTargetId(String(row.target_mother_question_id || row.question_index || ''));
      setEditingComment(String(row.review_comment || ''));
      setEditingManualStem(String(row.manual_question_stem || row.question_stem || ''));
      setEditingManualOptions(Array.isArray(row.manual_question_options) && row.manual_question_options.length
        ? row.manual_question_options.join('\n')
        : ((Array.isArray(row.question_options) ? row.question_options : []).join('\n')));
      setEditingManualExplanation(String(row.manual_question_explanation || row.question_explanation || ''));
      message.info('已进入修改状态，映射已置为待审核');
    } catch (e) {
      // onQuickConfirm already toast
    }
  };

  const onSaveMapping = async (row) => {
    const mapKey = String(row.map_key || '');
    if (!mapKey) return;
    setSavingEdit(true);
    try {
      const manualOptions = splitOptionsText(editingManualOptions);
      await batchReviewMappings(tenantId, {
        map_keys: [mapKey],
        confirm_status: 'pending',
        reviewer: systemUser,
        comment: editingComment || '',
        target_mother_question_id: editingTargetId || '',
        manual_question_stem: editingManualStem || '',
        manual_question_options: manualOptions,
        manual_question_explanation: editingManualExplanation || '',
        material_version_id: materialVersionId || undefined,
      });
      message.success(`映射 ${mapKey} 已保存，状态为待审核`);
      setEditingMapKey('');
      setEditingTargetId('');
      setEditingComment('');
      setEditingManualStem('');
      setEditingManualOptions('');
      setEditingManualExplanation('');
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '保存映射失败');
    } finally {
      setSavingEdit(false);
    }
  };

  const treeData = useMemo(() => {
    const root = [];
    const nodeMap = new Map();
    const ensureNode = (key, title, parentChildren, pathPrefix) => {
      if (!nodeMap.has(key)) {
        const n = { key, rawTitle: title, title, children: [], nodeType: 'path', pathPrefix, mapKeySet: new Set(), mapKeys: [] };
        nodeMap.set(key, n);
        parentChildren.push(n);
      }
      return nodeMap.get(key);
    };

    rows.forEach((row) => {
      const segs = String(row.path || '').split(' > ').map((x) => x.trim()).filter(Boolean);
      let parentChildren = root;
      let acc = [];
      if (!segs.length) segs.push('未分类');
      segs.forEach((seg) => {
        acc = [...acc, seg];
        const key = `path:${acc.join(' > ')}`;
        const node = ensureNode(key, seg, parentChildren, acc.join(' > '));
        node.mapKeySet.add(String(row.map_key));
        parentChildren = node.children;
      });
    });

    const normalize = (nodes) => {
      nodes.forEach((n) => {
        n.mapKeys = Array.from(n.mapKeySet).filter(Boolean);
        n.title = `${n.rawTitle} (${n.mapKeys.length})`;
        delete n.rawTitle;
        delete n.mapKeySet;
        if (n.children?.length) normalize(n.children);
      });
    };
    normalize(root);
    return root;
  }, [rows]);

  const scopedPreviewRows = useMemo(() => {
    if (!previewMapKeys.length) return [];
    const set = new Set(previewMapKeys.map((x) => String(x)));
    return rows
      .filter((x) => set.has(String(x.map_key)))
      .slice()
      .sort((a, b) => {
        const aSid = Number(a?.slice_id);
        const bSid = Number(b?.slice_id);
        const aQid = Number(a?.question_index);
        const bQid = Number(b?.question_index);
        if (Number.isFinite(aSid) && Number.isFinite(bSid) && aSid !== bSid) return aSid - bSid;
        if (Number.isFinite(aQid) && Number.isFinite(bQid) && aQid !== bQid) return aQid - bQid;
        return String(a?.map_key || '').localeCompare(String(b?.map_key || ''));
      });
  }, [rows, previewMapKeys]);

  const scopedReadyRows = useMemo(
    () => scopedPreviewRows.filter((row) => isRowReadyForReview(row)),
    [scopedPreviewRows]
  );

  const scopedUnreadyRows = useMemo(
    () => scopedPreviewRows.filter((row) => !isRowReadyForReview(row)),
    [scopedPreviewRows]
  );

  const checkedTreeKeys = useMemo(() => {
    const keys = [];
    const walk = (nodes) => {
      (nodes || []).forEach((n) => {
        if (Array.isArray(n.mapKeys) && n.mapKeys.some((mk) => selectedRowKeys.includes(String(mk)))) {
          keys.push(n.key);
        }
        walk(n.children || []);
      });
    };
    walk(treeData);
    return keys;
  }, [treeData, selectedRowKeys]);

  const onTreeCheck = (checkedKeys, info) => {
    const keys = Array.isArray(checkedKeys) ? checkedKeys : (checkedKeys?.checked || []);
    const checkedSet = new Set(keys.map((k) => String(k)));
    const mapKeys = new Set();
    const walk = (nodes) => {
      (nodes || []).forEach((n) => {
        if (checkedSet.has(String(n.key))) (n.mapKeys || []).forEach((mk) => mapKeys.add(String(mk)));
        walk(n.children || []);
      });
    };
    walk(treeData);
    setSelectedRowKeys(Array.from(mapKeys).filter(Boolean));
    const clickedNode = info?.node;
    if (clickedNode && Array.isArray(clickedNode.mapKeys) && clickedNode.mapKeys.length > 0) {
      setActiveMapKey(clickedNode.mapKeys[0]);
    }
  };

  return (
    <div className="slice-review-page">
      <Card className="slice-top-panel" style={{ marginBottom: 12 }}>
        <div className="slice-toolbar-inline">
          <Space className="toolbar slice-toolbar" wrap>
            <Select
              value={materialVersionId}
              style={{ width: 260 }}
              onChange={setMaterialVersionId}
              placeholder="教材版本"
              options={materials.map((m) => ({ label: materialLabel(m), value: m.material_version_id }))}
            />
            <Select
              value={filters.status}
              style={{ width: 170 }}
              onChange={(v) => setFilters((s) => ({ ...s, status: v }))}
              options={[
                { label: '全部状态', value: 'all' },
                { label: '待审核', value: 'pending' },
                { label: '已通过', value: 'approved' },
              ]}
            />
            <Select
              value={filters.meta_conflict}
              style={{ width: 180 }}
              onChange={(v) => setFilters((s) => ({ ...s, meta_conflict: v }))}
              options={[
                { label: '全部映射', value: 'all' },
                { label: '仅元数据冲突', value: 'yes' },
                { label: '仅正常映射', value: 'no' },
              ]}
            />
            <Button type="primary" onClick={() => loadData()}>查询</Button>
          </Space>
          <Typography.Text type="secondary" className="slice-toolbar-metrics">
            当前筛选映射 {metrics.total} ｜ 待审核 {metrics.pending} ｜ 已通过 {metrics.approved}
          </Typography.Text>
        </div>
      </Card>
      {mappingJob ? (
        <Alert
          style={{ marginBottom: 12 }}
          type={mappingJob.status === 'failed' ? 'error' : (mappingJob.status === 'completed' ? 'success' : 'info')}
          showIcon
          message={`映射任务：${mappingJob.material_name}（${mappingJob.progress}%）`}
          description={mappingJob.message || (mappingJob.status === 'running' ? '后台正在执行映射，请稍候…' : '')}
        />
      ) : null}

      {selectedRowKeys.length > 0 && (
        <Card className="slice-batch-card" style={{ marginBottom: 12 }}>
          <Typography.Text strong>批量确认</Typography.Text>
          <Alert type="info" showIcon message={`已选 ${selectedRowKeys.length} 条，可批量确认`} />
          <Form layout="inline" style={{ marginTop: 12 }} onFinish={onBatchSubmit}>
            <Form.Item name="confirm_status" initialValue="approved" rules={[{ required: true }]}>
              <Select
                style={{ width: 170 }}
                options={[
                  { label: '已通过', value: 'approved' },
                  { label: '待审核', value: 'pending' },
                ]}
              />
            </Form.Item>
            <Form.Item name="target_mother_question_id">
              <Input placeholder="remap目标母题ID" style={{ width: 180 }} />
            </Form.Item>
            <Form.Item name="comment">
              <Input placeholder="备注" style={{ width: 220 }} />
            </Form.Item>
            <Form.Item><Button type="primary" htmlType="submit">批量提交</Button></Form.Item>
          </Form>
        </Card>
      )}

      <Row gutter={12}>
        <Col span={6}>
          <Card
            className="slice-tree-card"
            title="教材目录树"
            loading={loading}
            extra={(
              <Space size={8}>
                <Tooltip title="目录树（不支持关键词输入）">
                  <Button size="small" icon={<SearchOutlined />} />
                </Tooltip>
                <Tooltip title={metrics.pending > 0 ? '' : '当前筛选下没有待审核映射'}>
                  <span>
                    <Button
                      size="small"
                      type="primary"
                      disabled={metrics.pending <= 0}
                      loading={approvingAll}
                      onClick={onApproveAllInCurrentTree}
                    >
                      全部通过
                    </Button>
                  </span>
                </Tooltip>
              </Space>
            )}
          >
            {treeData.length ? (
              <Tree
                checkable
                checkedKeys={checkedTreeKeys}
                onCheck={onTreeCheck}
                onSelect={(_, info) => {
                  const node = info?.node || {};
                  if (node.nodeType === 'path') {
                    const keys = Array.isArray(node.mapKeys) ? node.mapKeys : [];
                    const firstKey = keys[0];
                    if (firstKey !== null && firstKey !== undefined) setActiveMapKey(firstKey);
                    setPreviewMapKeys(keys);
                  }
                }}
                treeData={treeData}
                defaultExpandAll
                height={680}
              />
            ) : (
              <Empty description="暂无映射数据" />
            )}
          </Card>
        </Col>

        <Col span={18}>
          <Card
            className="slice-preview-card"
            title="内容预览"
            loading={loading}
            extra={(
              <Typography.Text type="secondary">
                当前目录映射：可审核 {scopedReadyRows.length} 条 ｜ 待补全 {scopedUnreadyRows.length} 条
              </Typography.Text>
            )}
          >
            {!activeMapKey && <Empty description="请在左侧目录树选择映射" />}
            {!!activeMapKey && (
              <div className="slice-preview-list">
                {scopedReadyRows.map((row) => {
                  const mk = String(row.map_key || '');
                  const isEditing = editingMapKey === mk;
                  return (
                    <div className="slice-preview-item" key={mk}>
                      <Space direction="vertical" style={{ width: '100%' }} size={8}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                          <Space style={{ minWidth: 0 }} size={8}>
                            <Tag color={statusColor[row.confirm_status] || 'default'}>{statusLabel[row.confirm_status] || row.confirm_status}</Tag>
                            {row.meta_conflict && <Tag color="orange">元数据冲突</Tag>}
                            {row.question_source === 'manual' && <Tag color="blue">手动母题</Tag>}
                            <Typography.Text strong style={{ minWidth: 0 }}>
                              {`Map: ${mk} | ${row.path || '（空路径）'}`}
                            </Typography.Text>
                          </Space>
                          <Space>
                            {row.confirm_status !== 'approved' && (
                              <Button type="primary" size="small" onClick={() => onQuickConfirm(mk, 'approved')}>
                                通过
                              </Button>
                            )}
                            {isEditing ? (
                              <Button size="small" type="primary" loading={savingEdit} onClick={() => onSaveMapping(row)}>
                                保存
                              </Button>
                            ) : (
                              <Button size="small" onClick={() => onEditMapping(row)}>修改</Button>
                            )}
                            <Button
                              size="small"
                              onClick={() => {
                                if (selectedRowKeys.includes(mk)) {
                                  setSelectedRowKeys((s) => s.filter((x) => x !== mk));
                                } else {
                                  setSelectedRowKeys((s) => [...s, mk]);
                                }
                              }}
                            >
                              {selectedRowKeys.includes(mk) ? '取消批量审核' : '加入批量审核'}
                            </Button>
                          </Space>
                        </div>
                        <Typography.Text>题目ID：{row.question_index ?? '（空）'} ｜ 置信度：{row.confidence ?? '（空）'}</Typography.Text>
                        {row.meta_conflict && (
                          <Typography.Text type="warning">
                            元数据冲突：{row.meta_conflict_detail || '路径字段与题干/解析可能不一致'}
                          </Typography.Text>
                        )}
                        {isEditing ? (
                          <Space direction="vertical" style={{ width: '100%' }}>
                            <Input
                              placeholder="目标母题ID（重映射）"
                              value={editingTargetId}
                              onChange={(e) => setEditingTargetId(e.target.value)}
                            />
                            <Input.TextArea
                              placeholder={'母题题干（手动填写后视为已关联）'}
                              value={editingManualStem}
                              rows={3}
                              onChange={(e) => setEditingManualStem(e.target.value)}
                            />
                            <Input.TextArea
                              placeholder={'母题选项（每行一个，支持 A. 前缀）'}
                              value={editingManualOptions}
                              rows={4}
                              onChange={(e) => setEditingManualOptions(e.target.value)}
                            />
                            <Input.TextArea
                              placeholder={'母题解析（手动填写）'}
                              value={editingManualExplanation}
                              rows={4}
                              onChange={(e) => setEditingManualExplanation(e.target.value)}
                            />
                            <Input
                              placeholder="备注"
                              value={editingComment}
                              onChange={(e) => setEditingComment(e.target.value)}
                            />
                          </Space>
                        ) : (
                          <Typography.Text type="secondary">
                            目标母题ID：{row.question_index ?? '（空）'}
                          </Typography.Text>
                        )}
                        <Typography.Text strong>切片内容（完整）</Typography.Text>
                        <div style={{ maxHeight: 360, overflow: 'auto' }}>
                          <MarkdownWithMermaid
                            text={injectImageLinksToMarkdown(
                              String(row.slice_content || row.slice_preview || '（空）'),
                              row,
                              tenantId,
                              materialVersionId
                            )}
                            disableStrikethrough
                            plainText
                          />
                        </div>
                        {!!(row.images || []).length && (
                          <>
                            <Typography.Text strong>{`切片图片（${(row.images || []).length}）`}</Typography.Text>
                            <Space direction="vertical" style={{ width: '100%' }} size={6}>
                              {(row.images || []).map((img, idx) => {
                                const p = String(img?.image_path || '').trim();
                                const id = String(img?.image_id || '').trim();
                                const title = id || p.split('/').pop() || `图片${idx + 1}`;
                                if (!p) return null;
                                return (
                                  <div key={`${mk}_img_${idx}`}>
                                    <Button
                                      size="small"
                                      onClick={async () => {
                                        try {
                                          const { blob, contentType } = await fetchSliceImageBlob(
                                            tenantId,
                                            p,
                                            row.material_version_id || materialVersionId
                                          );
                                          if (!blob || blob.size <= 0) throw new Error('empty_blob');
                                          const ct = String(contentType || '').toLowerCase();
                                          if (ct.includes('application/json') || ct.includes('text/html') || ct.includes('text/plain')) {
                                            throw new Error(`invalid_content_type:${contentType || 'unknown'}`);
                                          }
                                          const blobUrl = window.URL.createObjectURL(blob);
                                          window.open(blobUrl, '_blank', 'noopener,noreferrer');
                                          window.setTimeout(() => window.URL.revokeObjectURL(blobUrl), 5 * 60 * 1000);
                                        } catch (err) {
                                          message.error(err?.response?.data?.error?.message || err?.message || '图片加载失败');
                                        }
                                      }}
                                    >
                                      {title}
                                    </Button>
                                    {extractMermaid(String(img?.analysis || '')) && (
                                      <Button
                                        size="small"
                                        style={{ marginLeft: 8 }}
                                        onClick={() => {
                                          setPreviewMermaid({
                                            open: true,
                                            code: extractMermaid(String(img?.analysis || '')),
                                            title: `${title}（Mermaid 放大）`,
                                            zoom: 140,
                                          });
                                        }}
                                      >
                                        放大Mermaid
                                      </Button>
                                    )}
                                    <SliceImagePreview
                                      tenantId={tenantId}
                                      imagePath={p}
                                      materialVersionId={row.material_version_id || materialVersionId}
                                    />
                                    {(() => {
                                      const analysisText = String(img?.analysis || '（无图片解析）');
                                      const useMarkdownTable = Boolean(img?.contains_table) || hasMarkdownTable(analysisText);
                                      if (useMarkdownTable) {
                                        return <MarkdownWithMermaid text={analysisText} disableStrikethrough />;
                                      }
                                      return (
                                        <pre
                                          style={{
                                            marginTop: 6,
                                            whiteSpace: 'pre-wrap',
                                            wordBreak: 'break-word',
                                            fontSize: 13,
                                            lineHeight: 1.6,
                                            background: '#fafafa',
                                            border: '1px solid #f0f0f0',
                                            borderRadius: 6,
                                            padding: 10,
                                          }}
                                        >
                                          {analysisText}
                                        </pre>
                                      );
                                    })()}
                                  </div>
                                );
                              })}
                            </Space>
                          </>
                        )}
                        <Typography.Text strong>题目内容（完整）</Typography.Text>
                        <Typography.Paragraph style={{ whiteSpace: 'pre-line', marginBottom: 0 }}>
                          {row.question_stem || '（空）'}
                        </Typography.Paragraph>
                        <Typography.Text strong>选项（完整）</Typography.Text>
                        <Typography.Paragraph style={{ whiteSpace: 'pre-line', marginBottom: 0 }}>
                          {(Array.isArray(row.question_options) ? row.question_options : [])
                            .map((opt, idx) => `${String.fromCharCode(65 + idx)}. ${opt}`)
                            .join('\n') || '（空）'}
                        </Typography.Paragraph>
                        <Typography.Text strong>正确答案</Typography.Text>
                        <Typography.Paragraph style={{ whiteSpace: 'pre-line', marginBottom: 0 }}>
                          {row.question_answer || '（空）'}
                        </Typography.Paragraph>
                        <Typography.Text strong>解析（完整）</Typography.Text>
                        <Typography.Paragraph style={{ whiteSpace: 'pre-line', marginBottom: 0 }}>
                          {row.question_explanation || '（空）'}
                        </Typography.Paragraph>
                      </Space>
                    </div>
                  );
                })}
                {!!scopedUnreadyRows.length && (
                  <Card size="small" style={{ marginTop: 12 }}>
                    <Space direction="vertical" style={{ width: '100%' }} size={8}>
                      <Typography.Text strong>待补全母题内容（不进入审核）</Typography.Text>
                      {scopedUnreadyRows.map((row) => {
                        const mk = String(row.map_key || '');
                        const isEditing = editingMapKey === mk;
                        return (
                          <div key={`${mk}_unready`} style={{ border: '1px dashed #f0f0f0', borderRadius: 6, padding: 10 }}>
                            <Space direction="vertical" style={{ width: '100%' }} size={6}>
                              <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                                <Typography.Text>{`Map: ${mk} | ${row.path || '（空路径）'}`}</Typography.Text>
                                {isEditing ? (
                                  <Button size="small" type="primary" loading={savingEdit} onClick={() => onSaveMapping(row)}>保存</Button>
                                ) : (
                                  <Button size="small" onClick={() => onEditMapping(row)}>修改</Button>
                                )}
                              </Space>
                              <Typography.Text type="warning">
                                缺少：{(Array.isArray(row.review_missing_fields) ? row.review_missing_fields : ['题干', '选项', '解析']).join(' / ')}
                              </Typography.Text>
                            </Space>
                          </div>
                        );
                      })}
                    </Space>
                  </Card>
                )}
              </div>
            )}
          </Card>
        </Col>
      </Row>

      <Modal
        open={previewMermaid.open}
        title={previewMermaid.title || 'Mermaid 预览'}
        width={900}
        onCancel={() => setPreviewMermaid({ open: false, code: '', title: '', zoom: 120 })}
        footer={null}
      >
        <Space align="center" style={{ marginBottom: 12 }}>
          <Typography.Text type="secondary">缩放</Typography.Text>
          <Slider
            value={previewMermaid.zoom}
            min={60}
            max={180}
            style={{ width: 240 }}
            onChange={(v) => setPreviewMermaid((s) => ({ ...s, zoom: v }))}
          />
        </Space>
        <div style={{ transform: `scale(${previewMermaid.zoom / 100})`, transformOrigin: 'top left' }}>
          <MarkdownWithMermaid text={`\n\`\`\`mermaid\n${previewMermaid.code}\n\`\`\`\n`} />
        </div>
      </Modal>
    </div>
  );
}
