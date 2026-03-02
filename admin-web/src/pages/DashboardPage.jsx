import React, { useEffect, useMemo, useState } from 'react';
import { Button, Card, Col, List, Row, Space, Tag, Typography, message } from 'antd';
import {
  ArrowRightOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  LinkOutlined,
  RobotOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { useNavigate } from 'react-router-dom';
import { getTenantStats, listMaterials, listTenants } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const { Text, Title } = Typography;

export default function DashboardPage() {
  const navigate = useNavigate();
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [stats, setStats] = useState({
    slice_total: 0,
    slice_pending: 0,
    slice_approved: 0,
    slice_approval_rate: 0,
    mapping_total: 0,
    mapping_pending: 0,
    mapping_approved: 0,
    mapping_approval_rate: 0,
    material_total: 0,
    effective_material_version: '',
    bank_total_all: 0,
    bank_total_effective: 0,
    gen_7d_total: 0,
    gen_7d_success: 0,
    gen_7d_failed: 0,
    last_upload_at: '',
    last_generate_at: '',
    focus_events: [],
  });
  const [materials, setMaterials] = useState([]);
  const [tenantName, setTenantName] = useState('');

  const loadStats = async (tid) => {
    if (!tid) return;
    setLoading(true);
    try {
      const statRes = await getTenantStats(tid);
      const [materialRes] = await Promise.allSettled([listMaterials(tid)]);
      const materialItems = materialRes.status === 'fulfilled' ? (materialRes.value?.items || []) : [];
      setMaterials(materialItems);
      setStats({ ...(statRes || {}) });
      try {
        const tenantsRes = await listTenants();
        const current = (tenantsRes?.items || []).find((t) => t.tenant_id === tid);
        setTenantName(current?.name || '');
      } catch {
        setTenantName('');
      }
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '加载统计失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  useEffect(() => {
    if (!tenantId) return;
    loadStats(tenantId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  const todoItems = useMemo(() => {
    const items = Array.isArray(stats.focus_events) ? stats.focus_events : [];
    if (items.length) return items.slice(0, 4);
    return [{ text: '当前链路正常，可以继续出题或抽检题库。', path: '/question-bank', action: '去题库' }];
  }, [stats]);

  const formatTime = (s) => {
    if (!s) return '暂无';
    try {
      return new Date(s).toLocaleString('zh-CN', { hour12: false });
    } catch {
      return s;
    }
  };

  const quickActions = [
    { key: '/materials', label: '资源上传', desc: '上传教材与参考题', icon: <UploadOutlined /> },
    { key: '/slice-review', label: '切片核对', desc: '处理待审核切片', icon: <FileSearchOutlined /> },
    { key: '/mapping-review', label: '映射确认', desc: '确认题目映射关系', icon: <LinkOutlined /> },
    { key: '/ai-generate', label: 'AI出题', desc: '按已审切片生成题目', icon: <RobotOutlined /> },
    { key: '/question-bank', label: '题库', desc: '查看与抽检题目', icon: <DatabaseOutlined /> },
  ];

  const recentMaterials = [...materials]
    .sort((a, b) => String(b.material_version_id || '').localeCompare(String(a.material_version_id || '')))
    .slice(0, 4);

  const dashboardTitle = `${tenantName || tenantId || '当前城市'}练习管理工作台`;

  return (
    <div className="dashboard-lite">
      <Card className="dashboard-lite-hero" loading={loading}>
        <div className="dashboard-lite-head">
          <div>
            <Title level={4} style={{ margin: '4px 0 0' }}>{dashboardTitle}</Title>
          </div>
          <Space>
            <Button onClick={() => loadStats(tenantId)}>刷新</Button>
          </Space>
        </div>

        <div className="dashboard-lite-kpis">
          <div className="dashboard-lite-kpi"><Text type="secondary">教材版本（总）</Text><Title level={3}>{stats.material_total || 0}</Title></div>
          <div className="dashboard-lite-kpi"><Text type="secondary">切片总数</Text><Title level={3}>{stats.slice_total || 0}</Title></div>
          <div className="dashboard-lite-kpi"><Text type="secondary">映射总数</Text><Title level={3}>{stats.mapping_total || 0}</Title></div>
          <div className="dashboard-lite-kpi"><Text type="secondary">题库（本教材）</Text><Title level={3}>{stats.bank_total_effective || 0}</Title></div>
        </div>

        <Text type="secondary">生效教材：{stats.effective_material_version || '未设置'}</Text>
      </Card>

      <Row gutter={12} style={{ marginTop: 12 }}>
        <Col xs={24} lg={8}>
          <Card title="流程进度（核心）" loading={loading}>
            <Space direction="vertical" size={6}>
              <Text>切片：待审核 {stats.slice_pending || 0} ｜ 已通过 {stats.slice_approved || 0} ｜ 通过率 {stats.slice_approval_rate || 0}%</Text>
              <Text>映射：待审核 {stats.mapping_pending || 0} ｜ 已通过 {stats.mapping_approved || 0} ｜ 通过率 {stats.mapping_approval_rate || 0}%</Text>
              <Text>可出题切片：{stats.slice_approved || 0}</Text>
              <Text>题库总量（本城市）：{stats.bank_total_all || 0}</Text>
            </Space>
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="质量与风险（预警）" loading={loading}>
            <Space direction="vertical" size={6}>
              <Text>近7天出题：{stats.gen_7d_total || 0} 次</Text>
              <Text>近7天成功：{stats.gen_7d_success || 0} 次</Text>
              <Text>近7天失败：{stats.gen_7d_failed || 0} 次</Text>
              <Text>高优先级待办：{(stats.slice_pending || 0) + (stats.mapping_pending || 0)} 项</Text>
            </Space>
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card title="运营效率（管理）" loading={loading}>
            <Space direction="vertical" size={6}>
              <Text>最近上传教材：{formatTime(stats.last_upload_at)}</Text>
              <Text>最近出题时间：{formatTime(stats.last_generate_at)}</Text>
              <Text>当前生效教材：{stats.effective_material_version || '未设置'}</Text>
            </Space>
          </Card>
        </Col>
      </Row>

      <Row gutter={12} style={{ marginTop: 12 }}>
        <Col xs={24} lg={16}>
          <Card title="重点事件" loading={loading} className="dashboard-equal-card">
            <List
              dataSource={todoItems}
              renderItem={(item, idx) => (
                <List.Item
                  actions={[<Button key={item.path} type="link" onClick={() => navigate(item.path)}>{item.action}</Button>]}
                >
                  <Space>
                    <Tag bordered={false}>{idx + 1}</Tag>
                    <Text>{item.text}</Text>
                  </Space>
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24} lg={8}>
          <Card title="快捷入口" loading={loading} className="dashboard-equal-card">
            <div className="dashboard-quick-grid">
              {quickActions.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className="dashboard-quick-item"
                  onClick={() => navigate(item.key)}
                >
                  <span className="dashboard-quick-icon">{item.icon}</span>
                  <span className="dashboard-quick-main">
                    <span className="dashboard-quick-label">{item.label}</span>
                    <span className="dashboard-quick-desc">{item.desc}</span>
                  </span>
                  <ArrowRightOutlined className="dashboard-quick-arrow" />
                </button>
              ))}
            </div>
          </Card>
        </Col>
      </Row>

      <Card title="最近教材版本" loading={loading} style={{ marginTop: 12 }}>
        <List
          dataSource={recentMaterials}
          locale={{ emptyText: '暂无教材版本' }}
          renderItem={(item) => (
            <List.Item>
              <Space direction="vertical" size={2} style={{ width: '100%' }}>
                <Space wrap>
                  <Text strong>{item.material_version_id}</Text>
                  {item.status === 'effective' ? <Tag color="green">生效</Tag> : <Tag>非生效</Tag>}
                  {item.mapping_ready ? <Tag color="blue">映射已生成</Tag> : <Tag>映射未生成</Tag>}
                </Space>
                <Text type="secondary">{String(item.file_path || '').split('/').pop() || '未命名教材'}</Text>
              </Space>
            </List.Item>
          )}
        />
      </Card>
    </div>
  );
}
