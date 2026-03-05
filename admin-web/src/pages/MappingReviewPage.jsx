import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Col, Empty, Form, Input, Modal, Row, Select, Space, Tag, Tree, Typography, message } from 'antd';
import { batchReviewMappings, fetchSliceImageBlob, getMappings, listMaterials, getSystemUser } from '../services/api';
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
  const [editingMapKey, setEditingMapKey] = useState('');
  const [editingTargetId, setEditingTargetId] = useState('');
  const [editingComment, setEditingComment] = useState('');
  const [savingEdit, setSavingEdit] = useState(false);
  const systemUser = getSystemUser();

  const materialLabel = (m) => {
    const raw = String(m?.file_path || '').split('/').pop() || '';
    const name = raw.replace(/^v\d{8}_\d{6}_/, '') || raw || m?.material_version_id;
    return `${name}${m?.status === 'effective' ? '（当前生效）' : ''}`;
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
    listMaterials(tenantId)
      .then((res) => {
        const items = res.items || [];
        setMaterials(items);
        const effective = items.find((x) => x.status === 'effective');
        setMaterialVersionId((effective || items[0] || {}).material_version_id || '');
      })
      .catch(() => {
        setMaterials([]);
        setMaterialVersionId('');
      });
  }, [tenantId]);

  const metrics = useMemo(() => {
    const total = rows.length;
    const pending = rows.filter((x) => x.confirm_status === 'pending').length;
    const approved = rows.filter((x) => x.confirm_status === 'approved').length;
    return { total, pending, approved };
  }, [rows]);

  const onBatchSubmit = async (values) => {
    if (!selectedRowKeys.length) {
      message.warning('请先选择记录');
      return;
    }
    try {
      await batchReviewMappings(tenantId, {
        map_keys: selectedRowKeys,
        confirm_status: values.confirm_status,
        comment: values.comment || '',
        reviewer: systemUser,
        target_mother_question_id: values.target_mother_question_id || '',
        material_version_id: materialVersionId || undefined,
      });
      message.success(`已更新 ${selectedRowKeys.length} 条映射`);
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '批量确认失败');
    }
  };

  const onQuickConfirm = async (mapKey, confirmStatus = 'approved') => {
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
      setEditingTargetId(String(row.question_index || ''));
      setEditingComment(String(row.review_comment || ''));
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
      await batchReviewMappings(tenantId, {
        map_keys: [mapKey],
        confirm_status: 'pending',
        reviewer: systemUser,
        comment: editingComment || '',
        target_mother_question_id: editingTargetId || '',
        material_version_id: materialVersionId || undefined,
      });
      message.success(`映射 ${mapKey} 已保存，状态为待审核`);
      setEditingMapKey('');
      setEditingTargetId('');
      setEditingComment('');
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
    return rows.filter((x) => set.has(String(x.map_key)));
  }, [rows, previewMapKeys]);

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
            当前页映射 {metrics.total} ｜ 待审核 {metrics.pending} ｜ 已通过 {metrics.approved}
          </Typography.Text>
        </div>
      </Card>

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
            extra={<Typography.Text type="secondary">当前目录下切片：{scopedPreviewRows.length} 条</Typography.Text>}
          >
            {!activeMapKey && <Empty description="请在左侧目录树选择映射" />}
            {!!activeMapKey && (
              <div className="slice-preview-list">
                {scopedPreviewRows.map((row) => {
                  const mk = String(row.map_key || '');
                  const isEditing = editingMapKey === mk;
                  return (
                    <div className="slice-preview-item" key={mk}>
                      <Space direction="vertical" style={{ width: '100%' }} size={8}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                          <Space style={{ minWidth: 0 }} size={8}>
                            <Tag color={statusColor[row.confirm_status] || 'default'}>{statusLabel[row.confirm_status] || row.confirm_status}</Tag>
                            {row.meta_conflict && <Tag color="orange">元数据冲突</Tag>}
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
                        <Typography.Paragraph style={{ whiteSpace: 'pre-line', marginBottom: 0 }}>
                          {row.slice_content || row.slice_preview || '（空）'}
                        </Typography.Paragraph>
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
              </div>
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
}
