import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import { useNavigate, useParams } from 'react-router-dom';
import { getGenerateTask } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';
import MarkdownWithMermaid from '../components/MarkdownWithMermaid';
import QuestionDetailView from '../components/QuestionDetailView';

export default function AIGenerateTaskDetailPage() {
  const navigate = useNavigate();
  const { taskId } = useParams();
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [task, setTask] = useState({});

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  const loadDetail = async () => {
    if (!tenantId || !taskId) return;
    setLoading(true);
    try {
      const res = await getGenerateTask(tenantId, taskId);
      setTask(res?.task || {});
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '加载任务详情失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, taskId]);

  const items = useMemo(() => (Array.isArray(task?.items) ? task.items : []), [task]);
  const processTrace = useMemo(() => (Array.isArray(task?.process_trace) ? task.process_trace : []), [task]);
  const errors = useMemo(() => (Array.isArray(task?.errors) ? task.errors : []), [task]);

  const columns = [
    { title: '题干', dataIndex: '题干', ellipsis: true },
    { title: '答案', dataIndex: '正确答案', width: 100, render: (v) => <Tag color="green">{v}</Tag> },
    { title: '难度值', dataIndex: '难度值', width: 100 },
    { title: '来源切片', dataIndex: '来源路径', ellipsis: true },
    {
      title: '查看',
      width: 100,
      render: (_, record) => (
        <details>
          <summary style={{ cursor: 'pointer' }}>查看</summary>
          <div style={{ marginTop: 8 }}>
            <QuestionDetailView question={record || {}} />
          </div>
        </details>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Card>
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Typography.Title level={5} style={{ margin: 0 }}>出题任务详情</Typography.Title>
          <Space>
            <Button onClick={loadDetail} loading={loading}>刷新</Button>
            <Button type="primary" onClick={() => navigate('/ai-generate')}>返回任务列表</Button>
          </Space>
        </Space>
      </Card>

      <Card loading={loading}>
        <Descriptions size="small" bordered column={2}>
          <Descriptions.Item label="任务ID">{String(task?.task_id || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="任务名称">{String(task?.task_name || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="状态">{String(task?.status || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="创建人">{String(task?.creator || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="创建时间">{String(task?.created_at || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="结束时间">{String(task?.ended_at || '') || '-'}</Descriptions.Item>
          <Descriptions.Item label="生成结果">{`${Number(task?.generated_count || 0)} / 入库 ${Number(task?.saved_count || 0)}`}</Descriptions.Item>
          <Descriptions.Item label="进度">{`${Number(task?.progress?.current || 0)}/${Number(task?.progress?.total || 0)}`}</Descriptions.Item>
        </Descriptions>
      </Card>

      <Card title="任务过程">
        <Collapse
          items={processTrace.map((item) => ({
            key: String(item.index),
            label: `第 ${item.index} 题 | 切片 ${item.slice_id} | 耗时 ${Math.max(0, Math.round((item.elapsed_ms || 0) / 1000))}s`,
            children: (
              <Space direction="vertical" style={{ width: '100%' }} size={8}>
                <Typography.Text type="secondary">{item.slice_path || '（无路径）'}</Typography.Text>
                <div style={{ maxHeight: 260, overflow: 'auto' }}>
                  <MarkdownWithMermaid text={String(item.slice_content || '（无切片内容）')} />
                </div>
                {(item.steps || []).map((step, idx) => (
                  <Typography.Paragraph key={`${item.index}_${idx}`} style={{ margin: 0 }}>
                    [{step?.node || 'system'}] {step?.message || ''} {step?.detail ? ` | ${step.detail}` : ''}
                  </Typography.Paragraph>
                ))}
              </Space>
            ),
          }))}
        />
      </Card>

      <Card title="题目结果">
        <Table
          rowKey={(record, idx) => `${taskId || 'task'}_${idx}`}
          columns={columns}
          dataSource={items}
          pagination={{ pageSize: 10 }}
        />
      </Card>

      {errors.length > 0 && (
        <Card title="错误信息">
          <Space direction="vertical" style={{ width: '100%' }}>
            {errors.map((e, i) => (
              <Alert key={i} type="error" message={String(e)} />
            ))}
          </Space>
        </Card>
      )}

    </Space>
  );
}
