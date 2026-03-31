import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Button, Card, Col, Empty, Input, Modal, Row, Select, Slider, Space, Tag, Tooltip, Tree, Typography, message } from 'antd';
import { MenuOutlined, SearchOutlined } from '@ant-design/icons';
import { ReactFlow, Background, Controls, addEdge, applyEdgeChanges, applyNodeChanges } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { addSlice, batchReviewSlices, exportSlicesExcel, fetchSliceImageBlob, getSliceImageUrl, getSlices, listMaterials, mergeSlices, reorderSlices, getSystemUser, updateSliceContent, updateSliceImageAnalysis } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';
import MarkdownWithMermaid from '../components/MarkdownWithMermaid';

const statusColor = { pending: 'default', approved: 'green' };
const statusLabel = { pending: '待审核', approved: '已通过' };
const INVISIBLE_SEG_RE = /[\u200b\u200c\u200d\ufeff\u2060]/g;
const escapeRegExp = (value) => String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const cleanPathSeg = (seg) => String(seg || '').replace(INVISIBLE_SEG_RE, '').trim();
const pathPrefix = (path, levels = 3) => String(path || '').split(' > ').map((x) => cleanPathSeg(x)).filter(Boolean).slice(0, levels).join(' > ');
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

const MindmapEditorModal = React.memo(({
  open,
  initialDirection,
  initialRfNodes,
  initialRfEdges,
  onCancel,
  onSave,
}) => {
  const [direction, setDirection] = useState('TD');
  const [rfNodes, setRfNodes] = useState([]);
  const [rfEdges, setRfEdges] = useState([]);
  const [selectedNodeId, setSelectedNodeId] = useState('');
  const [selectedEdgeId, setSelectedEdgeId] = useState('');
  const [draftLabel, setDraftLabel] = useState('');

  useEffect(() => {
    if (!open) return;
    setDirection(initialDirection || 'TD');
    setRfNodes(Array.isArray(initialRfNodes) ? initialRfNodes : []);
    setRfEdges(Array.isArray(initialRfEdges) ? initialRfEdges : []);
    setSelectedNodeId('');
    setSelectedEdgeId('');
    setDraftLabel('');
  }, [open, initialDirection, initialRfNodes, initialRfEdges]);

  const makeNewNodeId = (nodes) => {
    const used = new Set((nodes || []).map((n) => String(n?.id || '')));
    let i = 1;
    while (used.has(`N${i}`)) i += 1;
    return `N${i}`;
  };

  const onNodesChange = useCallback((changes) => {
    if (!Array.isArray(changes) || changes.length === 0) return;
    setRfNodes((prev) => applyNodeChanges(changes, prev));
  }, []);

  const onEdgesChange = useCallback((changes) => {
    if (!Array.isArray(changes) || changes.length === 0) return;
    setRfEdges((prev) => applyEdgeChanges(changes, prev));
  }, []);

  const onConnect = useCallback((conn) => {
    setRfEdges((prev) => {
      if (!conn?.source || !conn?.target) return prev;
      const dup = prev.some((e) => e.source === conn.source && e.target === conn.target);
      if (dup) return prev;
      return addEdge({ ...conn, id: `e_${conn.source}_${conn.target}_${Date.now()}` }, prev);
    });
  }, []);

  const onSelectionChange = useCallback(({ nodes, edges }) => {
    const nodeId = String(nodes?.[0]?.id || '');
    const edgeId = nodeId ? '' : String(edges?.[0]?.id || '');
    setSelectedNodeId(nodeId);
    setSelectedEdgeId(edgeId);
  }, []);

  return (
    <Modal
      open={open}
      title="脑图修改器"
      width={980}
      destroyOnClose
      okText="回填到解析"
      onOk={() => onSave({ direction, rfNodes, rfEdges })}
      onCancel={onCancel}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={10}>
        <Space wrap>
          <Typography.Text>方向</Typography.Text>
          <Select
            style={{ width: 120 }}
            value={direction}
            options={[
              { value: 'TD', label: 'TD 上下' },
              { value: 'LR', label: 'LR 左右' },
              { value: 'RL', label: 'RL 右左' },
              { value: 'BT', label: 'BT 下上' },
            ]}
            onChange={setDirection}
          />
          <Button
            onClick={() => {
              const id = makeNewNodeId(rfNodes);
              const label = String(draftLabel || '').trim() || id;
              setRfNodes((prev) => ([
                ...prev,
                {
                  id,
                  position: { x: 120 + (prev.length * 40), y: 120 + (prev.length * 20) },
                  data: { label },
                },
              ]));
              setDraftLabel('');
            }}
          >
            新增节点
          </Button>
          <Button
            danger
            disabled={!selectedNodeId && !selectedEdgeId}
            onClick={() => {
              if (selectedNodeId) {
                setRfNodes((prev) => prev.filter((n) => n.id !== selectedNodeId));
                setRfEdges((prev) => prev.filter((e) => e.source !== selectedNodeId && e.target !== selectedNodeId));
                setSelectedNodeId('');
                return;
              }
              if (selectedEdgeId) {
                setRfEdges((prev) => prev.filter((e) => e.id !== selectedEdgeId));
                setSelectedEdgeId('');
              }
            }}
          >
            删除选中
          </Button>
        </Space>
        <Space>
          <Input
            style={{ width: 260 }}
            placeholder="新节点名称（可选）"
            value={draftLabel}
            onChange={(e) => setDraftLabel(e.target.value)}
          />
          <Input
            style={{ width: 360 }}
            placeholder="选中节点后可改名"
            value={(() => {
              const node = rfNodes.find((n) => n.id === selectedNodeId);
              return String(node?.data?.label || '');
            })()}
            onChange={(e) => {
              const val = e.target.value;
              setRfNodes((prev) => prev.map((n) => (
                n.id === selectedNodeId ? { ...n, data: { ...(n.data || {}), label: val } } : n
              )));
            }}
            disabled={!selectedNodeId}
          />
          <Typography.Text type="secondary">操作：拖拽节点，拖拽节点锚点可连线，点选后可删除</Typography.Text>
        </Space>
        <div style={{ height: 520, border: '1px solid #f0f0f0', borderRadius: 6 }}>
          <ReactFlow
            nodes={rfNodes}
            edges={rfEdges}
            onlyRenderVisibleElements
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onSelectionChange={onSelectionChange}
          >
            <Controls />
            <Background />
          </ReactFlow>
        </div>
      </Space>
    </Modal>
  );
});

export default function SliceReviewPage() {
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [rows, setRows] = useState([]);
  const [selectedRowKeys, setSelectedRowKeys] = useState([]);
  const [mergeQueueIds, setMergeQueueIds] = useState([]);
  const [approveQueueIds, setApproveQueueIds] = useState([]);
  const [filters, setFilters] = useState({ status: 'all', sliceKeyword: '' });
  const [materials, setMaterials] = useState([]);
  const [materialVersionId, setMaterialVersionId] = useState('');
  const systemUser = getSystemUser();
  const [pagination, setPagination] = useState({ current: 1, pageSize: 200, total: 0 });
  const [exporting, setExporting] = useState(false);
  const [activeSliceId, setActiveSliceId] = useState(null);
  const [previewSliceIds, setPreviewSliceIds] = useState([]);
  const [editingSliceId, setEditingSliceId] = useState(null);
  const [editingContent, setEditingContent] = useState('');
  const [savingEdit, setSavingEdit] = useState(false);
  const [approvingAll, setApprovingAll] = useState(false);
  const [previewMermaid, setPreviewMermaid] = useState({ open: false, code: '', title: '', zoom: 120 });
  const [editingImage, setEditingImage] = useState({ open: false, sliceId: null, imageId: '', imagePath: '', analysis: '' });
  const [mindmapEditor, setMindmapEditor] = useState({
    open: false,
    initialDirection: 'TD',
    initialRfNodes: [],
    initialRfEdges: [],
  });
  const [savingImage, setSavingImage] = useState(false);
  const [merging, setMerging] = useState(false);
  const [selectedTreePathPrefix, setSelectedTreePathPrefix] = useState('');
  const [selectedTreeLevel, setSelectedTreeLevel] = useState(0);
  const [addModalOpen, setAddModalOpen] = useState(false);
  const [newSliceContent, setNewSliceContent] = useState('');
  const [newSliceMastery, setNewSliceMastery] = useState('');
  const [newSlicePathSuffix, setNewSlicePathSuffix] = useState('');
  const [addingSlice, setAddingSlice] = useState(false);
  const [dragSliceId, setDragSliceId] = useState(null);
  const [savingOrder, setSavingOrder] = useState(false);
  const previewListRef = useRef(null);
  const lastScrollRef = useRef(0);

  const formatSliceContent = (txt) => {
    return String(txt || '');
  };

  const openSliceImageInNewWindow = async (path, mid) => {
    const p = String(path || '').trim();
    if (!p) return;
    try {
      const { blob, contentType } = await fetchSliceImageBlob(tenantId, p, mid || materialVersionId);
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
  };

  const getImageLinkTargets = (row) => {
    const items = Array.isArray(row?.images) ? row.images : [];
    const map = new Map();
    items.forEach((img) => {
      const path = String(img?.image_path || '').trim();
      if (!path) return;
      const url = getSliceImageUrl(tenantId, path, row.material_version_id || materialVersionId);
      const basename = path.split('/').pop() || '';
      const imageId = String(img?.image_id || '').trim();
      map.set(path, { token: path, sourcePath: path, url, title: basename || path });
      if (basename) map.set(basename, { token: basename, sourcePath: path, url, title: basename });
      if (imageId && imageId !== basename) map.set(imageId, { token: imageId, sourcePath: path, url, title: imageId });
    });
    return Array.from(map.values()).sort((a, b) => b.token.length - a.token.length);
  };

  const injectImageLinksToMarkdown = (text, row) => {
    const content = String(text || '');
    if (!content) return '（空）';
    const targets = getImageLinkTargets(row);
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

  const extractMermaid = (text) => {
    const raw = String(text || '');
    const match = raw.match(/```mermaid\s*([\s\S]*?)```/);
    return match ? match[1].trim() : '';
  };

  const splitAnalysisByMermaid = (text) => {
    const raw = String(text || '');
    const match = raw.match(/```mermaid\s*([\s\S]*?)```/);
    if (!match) return { prefix: raw, code: '', suffix: '' };
    return {
      prefix: raw.slice(0, match.index),
      code: String(match[1] || '').trim(),
      suffix: raw.slice((match.index || 0) + match[0].length),
    };
  };

  const parseNodeToken = (token) => {
    let t = String(token || '').trim();
    t = t.replace(/^\|[^|]*\|\s*/, '').trim();
    if (!t) return null;
    const decodeLabel = (label) => String(label || '')
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, '\\');
    const mQuoted = t.match(/^([^\[\]\s]+)\["((?:[^"\\]|\\.)*)"\]$/);
    if (mQuoted) return { id: mQuoted[1].trim(), label: decodeLabel(mQuoted[2].trim()) };
    const m = t.match(/^([^\[\]\s]+)\[(.+)\]$/);
    if (m) return { id: m[1].trim(), label: decodeLabel(m[2].trim()) };
    const mCircle = t.match(/^([^\(\)\s]+)\(\((.+?)\)\)$/);
    if (mCircle) return { id: mCircle[1].trim(), label: mCircle[2].trim() };
    return { id: t, label: t };
  };

  const splitByTopLevelToken = (text, token) => {
    const s = String(text || '');
    const out = [];
    let quote = false;
    let square = 0;
    let round = 0;
    let start = 0;
    for (let i = 0; i < s.length; i += 1) {
      const ch = s[i];
      if (ch === '"' && s[i - 1] !== '\\') quote = !quote;
      if (!quote) {
        if (ch === '[') square += 1;
        else if (ch === ']') square = Math.max(0, square - 1);
        else if (ch === '(') round += 1;
        else if (ch === ')') round = Math.max(0, round - 1);
      }
      if (!quote && square === 0 && round === 0 && s.slice(i, i + token.length) === token) {
        out.push(s.slice(start, i).trim());
        i += token.length - 1;
        start = i + 1;
      }
    }
    out.push(s.slice(start).trim());
    return out.filter(Boolean);
  };

  const parseMermaidToGraph = (code) => {
    const lines = String(code || '').split('\n');
    const labels = new Map();
    const edges = [];
    let direction = 'TD';
    lines.forEach((line) => {
      const s = String(line || '').trim();
      if (!s || s.startsWith('%%')) return;
      const d = s.match(/^flowchart\s+(TD|LR|RL|BT)\b/i);
      if (d) {
        direction = d[1].toUpperCase();
        return;
      }
      if (s.startsWith('linkStyle') || s.startsWith('subgraph') || s === 'end') return;
      if (s.includes('-->')) {
        const parts = splitByTopLevelToken(s, '-->');
        if (parts.length >= 2) {
          for (let i = 0; i < parts.length - 1; i += 1) {
            const left = parseNodeToken(parts[i]);
            const right = parseNodeToken(parts[i + 1]);
            if (!left || !right) return;
            const leftLabel = String(left.label || left.id);
            const rightLabel = String(right.label || right.id);
            if (!labels.has(left.id) || leftLabel !== left.id) labels.set(left.id, leftLabel);
            if (!labels.has(right.id) || rightLabel !== right.id) labels.set(right.id, rightLabel);
            edges.push([left.id, right.id]);
          }
          return;
        }
      }
      const n = parseNodeToken(s);
      if (n) labels.set(n.id, n.label || n.id);
    });
    const nodes = Array.from(labels.entries()).map(([id, label]) => ({ id, label }));
    const dedup = [];
    const seen = new Set();
    edges.forEach(([a, b]) => {
      const k = `${a}=>${b}`;
      if (a && b && !seen.has(k)) {
        seen.add(k);
        dedup.push({ from: a, to: b });
      }
    });
    return { direction, nodes, edges: dedup };
  };

  const buildMermaidFromGraph = (direction, nodes, edges) => {
    const encodeLabel = (label) => String(label || '')
      .replace(/\\/g, '\\\\')
      .replace(/"/g, '\\"')
      .replace(/\r?\n/g, '<br/>');
    const toSafeId = (raw, idx) => {
      const baseRaw = String(raw || '').trim() || `N_${idx + 1}`;
      let base = baseRaw.replace(/[^A-Za-z0-9_]/g, '_');
      if (!base) base = `N_${idx + 1}`;
      if (!/^[A-Za-z_]/.test(base)) base = `N_${base}`;
      return base;
    };
    const safeNodes = Array.isArray(nodes) ? nodes.filter((n) => n && String(n.id || '').trim()) : [];
    const idMap = new Map();
    const used = new Set();
    safeNodes.forEach((n, idx) => {
      const srcId = String(n.id);
      let cand = toSafeId(srcId, idx);
      if (used.has(cand)) {
        let i = 2;
        while (used.has(`${cand}_${i}`)) i += 1;
        cand = `${cand}_${i}`;
      }
      used.add(cand);
      idMap.set(srcId, cand);
    });
    const labelBySafeId = new Map(
      safeNodes.map((n) => [idMap.get(String(n.id)), String(n.label || n.id)])
    );
    const out = [`flowchart ${direction || 'TD'}`];
    const edgeSeen = new Set();
    const usedInEdge = new Set();
    const safeEdgeLines = [];
    (edges || []).forEach((e) => {
      const fromSrc = String(e?.from || '').trim();
      const toSrc = String(e?.to || '').trim();
      const a = idMap.get(fromSrc);
      const b = idMap.get(toSrc);
      if (!a || !b) return;
      const k = `${a}=>${b}`;
      if (edgeSeen.has(k)) return;
      edgeSeen.add(k);
      usedInEdge.add(a);
      usedInEdge.add(b);
      safeEdgeLines.push(`  ${a} --> ${b}`);
    });

    // Always declare all nodes explicitly so isolated nodes are persisted reliably.
    Array.from(labelBySafeId.entries()).forEach(([sid, label]) => {
      out.push(`  ${sid}["${encodeLabel(label)}"]`);
    });
    safeEdgeLines.forEach((line) => out.push(line));
    return out.join('\n');
  };

  const layoutGraph = (nodes, edges, direction = 'TD') => {
    const ids = nodes.map((n) => n.id);
    const indeg = new Map(ids.map((id) => [id, 0]));
    const outs = new Map(ids.map((id) => [id, []]));
    edges.forEach((e) => {
      const s = String(e?.from || '');
      const t = String(e?.to || '');
      if (!indeg.has(s) || !indeg.has(t)) return;
      indeg.set(t, (indeg.get(t) || 0) + 1);
      outs.get(s).push(t);
    });
    const level = new Map(ids.map((id) => [id, 0]));
    const queue = ids.filter((id) => (indeg.get(id) || 0) === 0);
    const seen = new Set();
    while (queue.length) {
      const cur = queue.shift();
      if (!cur || seen.has(cur)) continue;
      seen.add(cur);
      const base = level.get(cur) || 0;
      (outs.get(cur) || []).forEach((nxt) => {
        level.set(nxt, Math.max(level.get(nxt) || 0, base + 1));
        indeg.set(nxt, (indeg.get(nxt) || 0) - 1);
        if ((indeg.get(nxt) || 0) <= 0) queue.push(nxt);
      });
    }
    const groups = new Map();
    ids.forEach((id) => {
      const lv = level.get(id) || 0;
      if (!groups.has(lv)) groups.set(lv, []);
      groups.get(lv).push(id);
    });
    const pos = new Map();
    Array.from(groups.keys()).sort((a, b) => a - b).forEach((lv) => {
      const arr = groups.get(lv) || [];
      arr.forEach((id, idx) => {
        const main = lv * 280;
        const cross = idx * 120;
        if (direction === 'LR' || direction === 'RL') {
          pos.set(id, { x: main, y: cross });
        } else {
          pos.set(id, { x: cross, y: main });
        }
      });
    });
    return nodes.map((n) => ({
      id: n.id,
      position: pos.get(n.id) || { x: 0, y: 0 },
      data: { label: n.label || n.id },
      draggable: true,
    }));
  };

  const graphToRf = (direction, nodes, edges) => ({
    rfNodes: layoutGraph(nodes, edges, direction),
    rfEdges: (edges || []).map((e, i) => ({
      id: `e_${String(e.from)}_${String(e.to)}_${i}`,
      source: String(e.from),
      target: String(e.to),
    })),
  });

  const rfToGraph = (rfNodes, rfEdges) => ({
    nodes: (rfNodes || []).map((n) => ({
      id: String(n.id),
      label: String(n?.data?.label || n.id),
    })),
    edges: (rfEdges || []).map((e) => ({
      from: String(e.source || ''),
      to: String(e.target || ''),
    })).filter((e) => e.from && e.to),
  });

  const materialLabel = (m) => {
    const raw = String(m?.file_path || '').split('/').pop() || '';
    const name = raw.replace(/^v\d{8}_\d{6}_/, '') || raw || m?.material_version_id;
    return `${name}${m?.status === 'effective' ? '（生效）' : ''}`;
  };

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  const loadData = async () => {
    if (!tenantId) return;
    setLoading(true);
    setLoadError('');
    try {
      if (previewListRef.current) {
        lastScrollRef.current = previewListRef.current.scrollTop || 0;
      }
      let page = 1;
      const all = [];
      let total = 0;
      while (true) {
        // eslint-disable-next-line no-await-in-loop
        const res = await getSlices(tenantId, {
          status: filters.status,
          keyword: filters.sliceKeyword,
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
      const items = all;
      setRows(items);
      setPagination({
        current: 1,
        pageSize: 200,
        total: items.length,
      });
      setSelectedRowKeys([]);
      const currentId = activeSliceId;
      const idSet = new Set(items.map((x) => Number(x.slice_id)));
      const nextActive = currentId !== null && idSet.has(Number(currentId)) ? currentId : (items[0]?.slice_id ?? null);
      setActiveSliceId(nextActive);
      if (currentId !== null && idSet.has(Number(currentId))) {
        setPreviewSliceIds((prev) => (prev && prev.length ? prev : items.map((x) => Number(x.slice_id)).filter((x) => Number.isFinite(x))));
      } else {
        setPreviewSliceIds(items.map((x) => Number(x.slice_id)).filter((x) => Number.isFinite(x)));
      }
      setTimeout(() => {
        if (previewListRef.current) {
          previewListRef.current.scrollTop = lastScrollRef.current || 0;
        }
      }, 0);
    } catch (e) {
      const errMsg = e?.response?.data?.error?.message || e?.message || '加载切片失败';
      setLoadError(errMsg);
      message.error(errMsg);
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
        const visibleItems = items.filter((x) => (
          String(x?.status || '') !== 'archived' && String(x?.slice_status || '') === 'success'
        ));
        setMaterials(visibleItems);
        const effective = visibleItems.find((x) => x.status === 'effective');
        setMaterialVersionId((effective || visibleItems[0] || {}).material_version_id || '');
      })
      .catch(() => {
        setMaterials([]);
        setMaterialVersionId('');
      });
  }, [tenantId]);

  const metrics = useMemo(() => {
    const total = rows.length;
    const pending = rows.filter((x) => x.review_status === 'pending').length;
    const approved = rows.filter((x) => x.review_status === 'approved').length;
    return { total, pending, approved };
  }, [rows]);

  const rowMap = useMemo(() => {
    const m = new Map();
    rows.forEach((r) => m.set(Number(r.slice_id), r));
    return m;
  }, [rows]);

  const canAddOrReorder = selectedTreeLevel === 3 && !!selectedTreePathPrefix;
  const canOneClickMerge = canAddOrReorder && mergeQueueIds.length >= 2;
  const canBatchApprove = approveQueueIds.length >= 2;

  useEffect(() => {
    if (!selectedTreePathPrefix || !rows.length) return;
    const ids = rows
      .filter((x) => String(x.path || '').startsWith(String(selectedTreePathPrefix || '')))
      .map((x) => Number(x.slice_id))
      .filter((x) => Number.isFinite(x));
    setPreviewSliceIds(ids);
    setActiveSliceId((prev) => {
      const prevId = Number(prev);
      if (Number.isFinite(prevId) && ids.includes(prevId)) return prev;
      return ids.length > 0 ? ids[0] : null;
    });
  }, [rows, selectedTreePathPrefix]);

  const onBatchApprove = async () => {
    if (approveQueueIds.length < 2) {
      message.warning('请先加入至少 2 条切片到审核队列');
      return;
    }
    try {
      const chunkSize = 200;
      for (let i = 0; i < approveQueueIds.length; i += chunkSize) {
        // eslint-disable-next-line no-await-in-loop
        await batchReviewSlices(tenantId, {
          slice_ids: approveQueueIds.slice(i, i + chunkSize),
          review_status: 'approved',
          reviewer: systemUser,
          material_version_id: materialVersionId || undefined,
        });
      }
      message.success(`已批量通过 ${approveQueueIds.length} 条切片`);
      setApproveQueueIds([]);
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '批量通过失败');
    }
  };

  const onApproveAllInCurrentTree = async () => {
    const ids = rows.map((x) => Number(x.slice_id)).filter((x) => Number.isFinite(x));
    if (!ids.length) {
      message.warning('当前筛选下没有可审核切片');
      return;
    }
    setApprovingAll(true);
    try {
      const chunkSize = 200;
      for (let i = 0; i < ids.length; i += chunkSize) {
        // eslint-disable-next-line no-await-in-loop
        await batchReviewSlices(tenantId, {
          slice_ids: ids.slice(i, i + chunkSize),
          review_status: 'approved',
          reviewer: systemUser,
          material_version_id: materialVersionId || undefined,
        });
      }
      message.success(`当前筛选下 ${ids.length} 条切片已全部通过`);
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '全部通过失败');
    } finally {
      setApprovingAll(false);
    }
  };

  const onMergeSelected = async () => {
    if (!canAddOrReorder) {
      message.warning('请先在左侧选中三级目录');
      return;
    }
    if (mergeQueueIds.length < 2) {
      message.warning('请先加入至少 2 条切片到合并队列');
      return;
    }
    const selectedRows = mergeQueueIds
      .map((sid) => rowMap.get(Number(sid)))
      .filter(Boolean)
      .sort((a, b) => Number(a.slice_id) - Number(b.slice_id));
    if (selectedRows.length < 2) {
      message.warning('有效切片不足 2 条');
      return;
    }
    setMerging(true);
    try {
      const res = await mergeSlices(tenantId, {
        material_version_id: materialVersionId || undefined,
        slice_ids: selectedRows.map((x) => Number(x.slice_id)),
        reviewer: systemUser,
      });
      message.success(`已合并 ${selectedRows.length} 条切片，仅保留 #${res?.base_slice_id}`);
      setMergeQueueIds([]);
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '合并切片失败');
    } finally {
      setMerging(false);
    }
  };

  const onQuickReview = async (sliceId, reviewStatus) => {
    const sid = Number(sliceId);
    const oldScrollTop = previewListRef.current?.scrollTop || 0;
    const scopedIds = scopedPreviewRows.map((x) => Number(x.slice_id)).filter((x) => Number.isFinite(x));
    const scopedIdx = scopedIds.indexOf(sid);
    const hiddenByCurrentFilter =
      (filters.status === 'pending' && reviewStatus === 'approved') ||
      (filters.status === 'approved' && reviewStatus === 'pending');
    try {
      await batchReviewSlices(tenantId, {
        slice_ids: [sliceId],
        review_status: reviewStatus,
        reviewer: systemUser,
        material_version_id: materialVersionId || undefined,
      });
      message.success(`切片 ${sliceId} 已更新为 ${reviewStatus}`);
      setRows((prev) => {
        let next = prev.map((r) => (Number(r.slice_id) === sid ? { ...r, review_status: reviewStatus } : r));
        if (hiddenByCurrentFilter) {
          next = next.filter((r) => Number(r.slice_id) !== sid);
        }
        return next;
      });
      if (hiddenByCurrentFilter) {
        setPreviewSliceIds((prev) => prev.map((x) => Number(x)).filter((x) => Number.isFinite(x) && x !== sid));
        setActiveSliceId((prev) => {
          if (Number(prev) !== sid) return prev;
          const remaining = scopedIds.filter((x) => x !== sid);
          const nextId = remaining[scopedIdx] ?? remaining[scopedIdx - 1] ?? null;
          return nextId;
        });
      }
      requestAnimationFrame(() => {
        if (previewListRef.current) {
          previewListRef.current.scrollTop = oldScrollTop;
        }
      });
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '快捷审核失败');
    }
  };

  const onEditSlice = async (row) => {
    const sid = Number(row.slice_id);
    if (!Number.isFinite(sid)) return;
    try {
      await onQuickReview(sid, 'pending');
      setEditingSliceId(sid);
      setEditingContent(row.slice_content || row.preview || '');
      message.info('已进入修改状态，切片已置为待审核，请保存后再审核通过');
    } catch (e) {
      // onQuickReview already handles error toast
    }
  };

  const onSaveSlice = async (row) => {
    const sid = Number(row.slice_id);
    if (!Number.isFinite(sid)) return;
    const content = String(editingContent || '').trim();
    if (!content) {
      message.warning('切片内容不能为空');
      return;
    }
    setSavingEdit(true);
    try {
      await updateSliceContent(tenantId, sid, {
        material_version_id: materialVersionId || undefined,
        slice_content: content,
      });
      message.success(`切片 ${sid} 已保存，当前状态为待审核，请审核通过后再出题`);
      setEditingSliceId(null);
      setEditingContent('');
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '保存切片失败');
    } finally {
      setSavingEdit(false);
    }
  };

  const onSaveImageAnalysis = async () => {
    if (!editingImage.open) return;
    const sliceId = editingImage.sliceId;
    if (sliceId === null || sliceId === undefined) return;
    setSavingImage(true);
    try {
      await updateSliceImageAnalysis(tenantId, sliceId, {
        material_version_id: materialVersionId || undefined,
        image_id: editingImage.imageId || undefined,
        image_path: editingImage.imagePath || undefined,
        analysis: editingImage.analysis || '',
      });
      setRows((prev) => prev.map((row) => {
        if (row.slice_id !== sliceId) return row;
        const imgs = Array.isArray(row.images) ? row.images.map((img) => {
          const id = String(img?.image_id || '').trim();
          const p = String(img?.image_path || '').trim();
          if ((editingImage.imageId && id === editingImage.imageId) || (editingImage.imagePath && p === editingImage.imagePath)) {
            return { ...img, analysis: editingImage.analysis };
          }
          return img;
        }) : [];
        return { ...row, images: imgs, review_status: 'pending' };
      }));
      message.success('图片解析已保存');
      setEditingImage({ open: false, sliceId: null, imageId: '', imagePath: '', analysis: '' });
    } catch (e) {
      message.error(e?.response?.data?.error?.message || e?.message || '保存图片解析失败');
    } finally {
      setSavingImage(false);
    }
  };

  const openMindmapEditor = () => {
    const code = extractMermaid(editingImage.analysis || '');
    if (!code) {
      message.warning('当前解析中没有 Mermaid 代码块');
      return;
    }
    const parsed = parseMermaidToGraph(code);
    if (!parsed.nodes.length) {
      message.warning('未解析到可编辑节点，请先在文本中补充标准 Mermaid');
      return;
    }
    const { rfNodes, rfEdges } = graphToRf(parsed.direction || 'TD', parsed.nodes, parsed.edges);
    setMindmapEditor({
      open: true,
      initialDirection: parsed.direction || 'TD',
      initialRfNodes: rfNodes,
      initialRfEdges: rfEdges,
    });
  };

  const saveMindmapToAnalysis = ({ direction, rfNodes, rfEdges }) => {
    const graph = rfToGraph(rfNodes, rfEdges);
    const code = buildMermaidFromGraph(direction, graph.nodes, graph.edges);
    const seg = splitAnalysisByMermaid(editingImage.analysis || '');
    const wrapped = `\n\`\`\`mermaid\n${code}\n\`\`\`\n`;
    const next = seg.code ? `${seg.prefix}${wrapped}${seg.suffix}` : `${String(editingImage.analysis || '').trim()}\n${wrapped}`;
    setEditingImage((s) => ({ ...s, analysis: next.trim() }));
    setMindmapEditor({
      open: false,
      initialDirection: 'TD',
      initialRfNodes: [],
      initialRfEdges: [],
    });
    message.success('已回填到解析文本，点击“保存”即可生效');
  };

  const onExportExcel = async () => {
    if (!tenantId) return;
    setExporting(true);
    try {
      const blob = await exportSlicesExcel(tenantId, {
        status: filters.status,
        keyword: filters.sliceKeyword,
        material_version_id: materialVersionId || undefined,
      });
      const ts = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15);
      const filename = `${tenantId}_切片导出_${ts}.xlsx`;
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      window.URL.revokeObjectURL(url);
      message.success('切片导出成功');
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '导出失败');
    } finally {
      setExporting(false);
    }
  };

  const onAddSliceSubmit = async () => {
    const content = String(newSliceContent || '').trim();
    if (!canAddOrReorder) {
      message.warning('请先在左侧选择一个三级目录');
      return;
    }
    if (!content) {
      message.warning('切片内容不能为空');
      return;
    }
    setAddingSlice(true);
    try {
      const suffix = String(newSlicePathSuffix || '').trim().replace(/^\s*>\s*/, '');
      const fullPath = suffix ? `${selectedTreePathPrefix} > ${suffix}` : selectedTreePathPrefix;
      const res = await addSlice(tenantId, {
        material_version_id: materialVersionId || undefined,
        path: fullPath,
        slice_content: content,
        mastery: String(newSliceMastery || '').trim(),
        reviewer: systemUser,
      });
      setAddModalOpen(false);
      setNewSliceContent('');
      setNewSliceMastery('');
      setNewSlicePathSuffix('');
      message.success(`已新增切片 #${res?.slice_id}`);
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '新增切片失败');
    } finally {
      setAddingSlice(false);
    }
  };

  const persistOrder = async (orderedIds) => {
    if (!canAddOrReorder) return;
    setSavingOrder(true);
    try {
      await reorderSlices(tenantId, {
        material_version_id: materialVersionId || undefined,
        path_prefix: selectedTreePathPrefix,
        slice_ids: orderedIds,
        reviewer: systemUser,
      });
      setPreviewSliceIds(orderedIds);
      message.success('切片顺序已保存');
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '保存顺序失败');
    } finally {
      setSavingOrder(false);
    }
  };

  const onDropSlice = async (targetId) => {
    if (!canAddOrReorder || dragSliceId === null) return;
    const fromId = Number(dragSliceId);
    const toId = Number(targetId);
    if (!Number.isFinite(fromId) || !Number.isFinite(toId) || fromId === toId) return;
    const ids = scopedPreviewRows.map((x) => Number(x.slice_id)).filter((x) => Number.isFinite(x));
    const fromIdx = ids.indexOf(fromId);
    const toIdx = ids.indexOf(toId);
    if (fromIdx < 0 || toIdx < 0) return;
    ids.splice(fromIdx, 1);
    ids.splice(toIdx, 0, fromId);
    setDragSliceId(null);
    await persistOrder(ids);
  };

  const treeData = useMemo(() => {
    const root = [];
    const nodeMap = new Map();
    const ensureNode = (key, title, parentChildren, pathPrefix, level) => {
      if (!nodeMap.has(key)) {
        const n = { key, rawTitle: title, title, children: [], nodeType: 'path', pathPrefix, level, sliceIdSet: new Set(), sliceIds: [] };
        nodeMap.set(key, n);
        parentChildren.push(n);
      }
      return nodeMap.get(key);
    };

    rows.forEach((row) => {
      const segs = String(row.path || '')
        .split(' > ')
        .map((x) => cleanPathSeg(x))
        .filter(Boolean)
        .slice(0, 3);
      let parentChildren = root;
      let acc = [];
      if (!segs.length) {
        segs.push('未分类');
      }
      segs.forEach((seg, idx) => {
        acc = [...acc, seg];
        const key = `path:${acc.join(' > ')}`;
        const node = ensureNode(key, seg, parentChildren, acc.join(' > '), idx + 1);
        node.sliceIdSet.add(Number(row.slice_id));
        parentChildren = node.children;
      });
    });

    const normalize = (nodes) => {
      nodes.forEach((n) => {
        n.sliceIds = Array.from(n.sliceIdSet).filter((x) => Number.isFinite(x));
        n.title = `${n.rawTitle} (${n.sliceIds.length})`;
        delete n.rawTitle;
        delete n.sliceIdSet;
        if (n.children?.length) normalize(n.children);
      });
    };
    normalize(root);
    return root;
  }, [rows]);

  const activeSlice = useMemo(
    () => rows.find((x) => Number(x.slice_id) === Number(activeSliceId)) || null,
    [rows, activeSliceId]
  );
  const scopedPreviewRows = useMemo(() => {
    if (!previewSliceIds.length) return [];
    const set = new Set(previewSliceIds.map((x) => Number(x)));
    return rows.filter((x) => set.has(Number(x.slice_id)));
  }, [rows, previewSliceIds]);

  useEffect(() => {
    const scopedSet = new Set(scopedPreviewRows.map((x) => Number(x.slice_id)));
    setMergeQueueIds((prev) => prev.filter((sid) => scopedSet.has(Number(sid))));
    setApproveQueueIds((prev) => prev.filter((sid) => scopedSet.has(Number(sid))));
  }, [scopedPreviewRows]);

  useEffect(() => {
    const approvedSet = new Set(
      rows
        .filter((x) => x.review_status === 'approved')
        .map((x) => Number(x.slice_id))
        .filter((x) => Number.isFinite(x))
    );
    setMergeQueueIds((prev) => prev.filter((sid) => !approvedSet.has(Number(sid))));
    setApproveQueueIds((prev) => prev.filter((sid) => !approvedSet.has(Number(sid))));
  }, [rows]);

  const checkedTreeKeys = useMemo(() => {
    const keys = [];
    const walk = (nodes) => {
      (nodes || []).forEach((n) => {
        if (Array.isArray(n.sliceIds) && n.sliceIds.some((id) => selectedRowKeys.includes(id))) {
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
    const ids = new Set();
    const walk = (nodes) => {
      (nodes || []).forEach((n) => {
        if (checkedSet.has(String(n.key))) {
          (n.sliceIds || []).forEach((sid) => ids.add(Number(sid)));
        }
        walk(n.children || []);
      });
    };
    walk(treeData);
    const selected = Array.from(ids).filter((x) => Number.isFinite(x));
    let targetP3 = pathPrefix(info?.node?.pathPrefix || '', 3);
    if (!targetP3 && selected.length > 0) {
      targetP3 = pathPrefix(rowMap.get(Number(selected[0]))?.path || '', 3);
    }
    const constrained = targetP3 ? selected.filter((sid) => pathPrefix(rowMap.get(Number(sid))?.path || '', 3) === targetP3) : selected;
    if (constrained.length !== selected.length) {
      message.warning('仅可选择同一一级/二级/三级目录下的切片');
    }
    setSelectedRowKeys(constrained);
    const clickedNode = info?.node;
    if (clickedNode && Array.isArray(clickedNode.sliceIds) && clickedNode.sliceIds.length > 0) {
      setActiveSliceId(clickedNode.sliceIds[0]);
      const lv = Number(clickedNode.level || 0);
      const pfx = String(clickedNode.pathPrefix || '');
      setSelectedTreeLevel(lv);
      setSelectedTreePathPrefix(pathPrefix(pfx, 3));
      if (lv >= 3) {
        setPreviewSliceIds(clickedNode.sliceIds.map((x) => Number(x)).filter((x) => Number.isFinite(x)));
      }
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
              options={materials.map((m) => ({
                label: materialLabel(m),
                value: m.material_version_id,
              }))}
            />
            <Select
              value={filters.status}
              style={{ width: 160 }}
              onChange={(v) => setFilters((s) => ({ ...s, status: v }))}
              options={[
                { label: '全部状态', value: 'all' },
                { label: '待审核', value: 'pending' },
                { label: '已通过', value: 'approved' },
              ]}
            />
            <Button type="primary" onClick={() => loadData()}>查询</Button>
            <Button loading={exporting} onClick={onExportExcel}>导出Excel</Button>
          </Space>
          <Typography.Text type="secondary" className="slice-toolbar-metrics">
            当前筛选切片 {metrics.total} ｜ 待审核 {metrics.pending} ｜ 已通过 {metrics.approved}
          </Typography.Text>
        </div>
      </Card>

      {!!loadError && (
        <Alert
          type="error"
          showIcon
          message="切片加载失败"
          description={loadError}
          style={{ marginBottom: 12 }}
        />
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
                <Tooltip title={metrics.pending > 0 ? '' : '当前筛选下没有待审核切片'}>
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
                checkStrictly
                checkedKeys={checkedTreeKeys}
                onCheck={onTreeCheck}
                onSelect={(_, info) => {
                  const node = info?.node || {};
                  if (node.nodeType === 'path') {
                    const ids = Array.isArray(node.sliceIds) ? node.sliceIds : [];
                    const firstId = ids[0];
                    if (firstId !== null && firstId !== undefined) setActiveSliceId(firstId);
                    setPreviewSliceIds(ids);
                    setSelectedTreeLevel(Number(node.level || 0));
                    setSelectedTreePathPrefix(pathPrefix(node.pathPrefix || '', 3));
                  }
                }}
                treeData={treeData}
                defaultExpandAll
                height={680}
              />
            ) : (
              <Empty description="暂无切片数据" />
            )}
          </Card>
        </Col>
        <Col span={18}>
          <Card
            className="slice-preview-card"
            title="内容预览"
            loading={loading}
            extra={(
              <Space size={10}>
                <Typography.Text type="secondary">当前目录下切片：{scopedPreviewRows.length} 条</Typography.Text>
                <Tooltip title={canAddOrReorder ? '' : '请先在左侧选中三级目录'}>
                  <span>
                    <Button
                      size="small"
                      type={canAddOrReorder ? 'primary' : 'default'}
                      disabled={!canAddOrReorder}
                      onClick={() => setAddModalOpen(true)}
                    >
                      新增
                    </Button>
                  </span>
                </Tooltip>
                <Tooltip
                  title={
                    !canAddOrReorder
                      ? '请先在左侧选中三级目录'
                      : (!canOneClickMerge ? '请先加入至少 2 条切片到合并队列' : '')
                  }
                >
                  <span>
                    <Button
                      type="primary"
                      size="small"
                      ghost={!canOneClickMerge}
                      loading={merging}
                      disabled={!canOneClickMerge}
                      onClick={onMergeSelected}
                    >
                      一键合并
                    </Button>
                  </span>
                </Tooltip>
                <Tooltip
                  title={
                    !canBatchApprove ? '请先加入至少 2 条切片到审核队列' : ''
                  }
                >
                  <span>
                    <Button
                      type="primary"
                      size="small"
                      loading={approvingAll}
                      disabled={!canBatchApprove}
                      onClick={async () => {
                        setApprovingAll(true);
                        try {
                          await onBatchApprove();
                        } finally {
                          setApprovingAll(false);
                        }
                      }}
                    >
                      批量通过
                    </Button>
                  </span>
                </Tooltip>
                {canAddOrReorder && <Typography.Text type="secondary">{savingOrder ? '正在保存顺序...' : '可拖拽调整顺序'}</Typography.Text>}
              </Space>
            )}
          >
            {!activeSlice && <Empty description="请在左侧目录树选择切片" />}
            {activeSlice && (
              <div className="slice-preview-list" ref={previewListRef}>
                {scopedPreviewRows.map((row) => {
                  const sid = Number(row.slice_id);
                  return (
                    <div
                      className="slice-preview-item"
                      key={row.slice_id}
                      draggable={canAddOrReorder}
                      style={canAddOrReorder ? { cursor: 'move' } : undefined}
                      onDragStart={() => setDragSliceId(sid)}
                      onDragOver={(e) => {
                        if (canAddOrReorder) e.preventDefault();
                      }}
                      onDrop={(e) => {
                        e.preventDefault();
                        onDropSlice(sid);
                      }}
                    >
                      <Space direction="vertical" style={{ width: '100%' }} size={8}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                          <Space style={{ minWidth: 0 }} size={8}>
                            {canAddOrReorder && <MenuOutlined style={{ color: '#8c8c8c' }} title="拖拽排序" />}
                            <Tag color={statusColor[row.review_status] || 'default'}>{statusLabel[row.review_status] || row.review_status}</Tag>
                            {row.generation_blocked && <Tag color="red">已禁用出题</Tag>}
                            {!row.generation_blocked && Number(row.generation_failure_count || 0) > 0 && (
                              <Tag color="orange">{`失败 ${Number(row.generation_failure_count || 0)} 次`}</Tag>
                            )}
                            <Typography.Text strong style={{ minWidth: 0 }}>
                              {`ID: ${row.slice_id} | ${row.path || '（空路径）'}`}
                            </Typography.Text>
                          </Space>
                          <Space>
                            <Tooltip title={canAddOrReorder ? '按住卡片拖拽可调整顺序' : '仅三级目录支持拖拽排序'}>
                              <Button size="small" icon={<MenuOutlined />} disabled={!canAddOrReorder}>
                                拖拽排序
                              </Button>
                            </Tooltip>
                            {row.review_status !== 'approved' && (
                              <Button
                                type="primary"
                                size="small"
                                onClick={() => onQuickReview(row.slice_id, 'approved')}
                              >
                                通过
                              </Button>
                            )}
                            {editingSliceId === sid ? (
                              <Button size="small" type="primary" loading={savingEdit} onClick={() => onSaveSlice(row)}>
                                保存
                              </Button>
                            ) : (
                              <Button size="small" onClick={() => onEditSlice(row)}>修改</Button>
                            )}
                            {row.review_status !== 'approved' && (
                              <Button
                                size="small"
                                disabled={!canAddOrReorder}
                                onClick={() => {
                                  if (!canAddOrReorder) return;
                                  setMergeQueueIds((prev) => (
                                    prev.includes(sid) ? prev.filter((x) => x !== sid) : [...prev, sid]
                                  ));
                                }}
                              >
                                {mergeQueueIds.includes(sid) ? '取消合并' : '加入合并'}
                              </Button>
                            )}
                            {row.review_status !== 'approved' && (
                              <Button
                                size="small"
                                onClick={() => {
                                  setApproveQueueIds((prev) => (
                                    prev.includes(sid) ? prev.filter((x) => x !== sid) : [...prev, sid]
                                  ));
                                }}
                              >
                                {approveQueueIds.includes(sid) ? '取消审核' : '加入审核'}
                              </Button>
                            )}
                          </Space>
                        </div>
                        {row.generation_blocked && (
                          <Alert
                            type="error"
                            showIcon
                            message="该切片已被禁止继续出题"
                            description={row.generation_block_reason || '该切片累计非白名单失败超过 10 次，需先修改切片后才能恢复出题。'}
                          />
                        )}
                        {!row.generation_blocked && Number(row.generation_failure_count || 0) > 0 && (
                          <Alert
                            type="warning"
                            showIcon
                            message={`该切片累计失败 ${Number(row.generation_failure_count || 0)} 次`}
                            description={row.generation_last_error_content || '最近一次失败未返回具体错误内容。'}
                          />
                        )}
                        <Typography.Text>掌握程度：{row.mastery || '（空）'}</Typography.Text>
                        {editingSliceId === sid ? (
                          <Input.TextArea
                            value={editingContent}
                            onChange={(e) => setEditingContent(e.target.value)}
                            autoSize={{ minRows: 6, maxRows: 14 }}
                          />
                        ) : (
                          <div style={{ maxHeight: 220, overflow: 'auto' }}>
                            <MarkdownWithMermaid
                              text={injectImageLinksToMarkdown(formatSliceContent(row.slice_content || row.preview), row)}
                              disableStrikethrough
                              plainText
                            />
                          </div>
                        )}
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
                                  <div key={`${sid}_img_${idx}`}>
                                    <Space size={8}>
                                      <a
                                        href={getSliceImageUrl(tenantId, p, row.material_version_id || materialVersionId)}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="slice-inline-image-link"
                                        onClick={(e) => {
                                          e.preventDefault();
                                          openSliceImageInNewWindow(p, row.material_version_id || materialVersionId);
                                        }}
                                      >
                                        🔗 {title}
                                      </a>
                                      <Button
                                        size="small"
                                        onClick={() => {
                                          openSliceImageInNewWindow(p, row.material_version_id || materialVersionId);
                                        }}
                                      >
                                        新页查看
                                      </Button>
                                    </Space>
                                    <SliceImagePreview
                                      tenantId={tenantId}
                                      imagePath={p}
                                      materialVersionId={row.material_version_id || materialVersionId}
                                    />
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
                                    <Button
                                      size="small"
                                      style={{ marginLeft: 8 }}
                                      onClick={() => {
                                        setEditingImage({
                                          open: true,
                                          sliceId: row.slice_id,
                                          imageId: String(img?.image_id || '').trim(),
                                          imagePath: String(img?.image_path || '').trim(),
                                          analysis: String(img?.analysis || ''),
                                        });
                                      }}
                                    >
                                      编辑解析
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
                      </Space>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </Col>
      </Row>
      <Modal
        open={addModalOpen}
        title="新增切片"
        onCancel={() => {
          setAddModalOpen(false);
          setNewSliceContent('');
          setNewSliceMastery('');
          setNewSlicePathSuffix('');
        }}
        onOk={onAddSliceSubmit}
        okButtonProps={{ loading: addingSlice, disabled: !canAddOrReorder }}
        okText="保存"
        cancelText="取消"
      >
        <Space direction="vertical" style={{ width: '100%' }} size={10}>
          <Typography.Text type="secondary">目录：{selectedTreePathPrefix || '请先选择三级目录'}</Typography.Text>
          <Input
            value={newSlicePathSuffix}
            onChange={(e) => setNewSlicePathSuffix(e.target.value)}
            placeholder="补充路径（可选，例如：三、市场周期波动）"
          />
          <Input
            value={newSliceMastery}
            onChange={(e) => setNewSliceMastery(e.target.value)}
            placeholder="掌握程度（可选）"
          />
          <Input.TextArea
            value={newSliceContent}
            onChange={(e) => setNewSliceContent(e.target.value)}
            autoSize={{ minRows: 8, maxRows: 16 }}
            placeholder="请输入切片内容"
          />
        </Space>
      </Modal>
      <Modal
        open={previewMermaid.open}
        title={previewMermaid.title || 'Mermaid 预览'}
        footer={null}
        onCancel={() => setPreviewMermaid({ open: false, code: '', title: '', zoom: 120 })}
        width={980}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={10}>
          <div>
            <Typography.Text>缩放：</Typography.Text>
            <Slider
              min={50}
              max={220}
              value={previewMermaid.zoom}
              onChange={(v) => setPreviewMermaid((s) => ({ ...s, zoom: v }))}
            />
          </div>
          <div style={{ border: '1px solid #eee', padding: 12, overflow: 'auto' }}>
            <div style={{ transform: `scale(${previewMermaid.zoom / 100})`, transformOrigin: 'top left' }}>
              <MarkdownWithMermaid text={`\n\`\`\`mermaid\n${previewMermaid.code}\n\`\`\`\n`} />
            </div>
          </div>
        </Space>
      </Modal>
      <Modal
        open={editingImage.open}
        title="编辑图片解析"
        okText="保存"
        onOk={onSaveImageAnalysis}
        okButtonProps={{ loading: savingImage }}
        onCancel={() => setEditingImage({ open: false, sliceId: null, imageId: '', imagePath: '', analysis: '' })}
        width={920}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={10}>
          <Typography.Text type="secondary">
            {editingImage.imageId || editingImage.imagePath || ''}
          </Typography.Text>
          <Input.TextArea
            value={editingImage.analysis}
            onChange={(e) => setEditingImage((s) => ({ ...s, analysis: e.target.value }))}
            autoSize={{ minRows: 8, maxRows: 20 }}
          />
          {extractMermaid(editingImage.analysis || '') ? (
            <Space>
              <Button size="small" onClick={openMindmapEditor}>脑图修改器</Button>
              <Typography.Text type="secondary">不会 Mermaid 可直接用脑图修改器调整节点和连线</Typography.Text>
            </Space>
          ) : null}
          <Typography.Text strong>预览</Typography.Text>
          <div style={{ border: '1px solid #f0f0f0', padding: 10 }}>
            {mindmapEditor.open ? (
              <Typography.Text type="secondary">脑图修改器已打开，暂停此处预览渲染以提升编辑流畅度。</Typography.Text>
            ) : (
              <MarkdownWithMermaid text={editingImage.analysis} />
            )}
          </div>
        </Space>
      </Modal>
      <MindmapEditorModal
        open={mindmapEditor.open}
        initialDirection={mindmapEditor.initialDirection}
        initialRfNodes={mindmapEditor.initialRfNodes}
        initialRfEdges={mindmapEditor.initialRfEdges}
        onSave={saveMindmapToAnalysis}
        onCancel={() => setMindmapEditor({
          open: false,
          initialDirection: 'TD',
          initialRfNodes: [],
          initialRfEdges: [],
        })}
      />
    </div>
  );
}
