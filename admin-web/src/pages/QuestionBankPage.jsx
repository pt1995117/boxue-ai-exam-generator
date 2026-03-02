import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Input, message, Modal, Popconfirm, Select, Space, Table } from 'antd';
import { deleteBankQuestions, exportBankQuestions, listBankQuestions, listMaterials } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';
import QuestionDetailView from '../components/QuestionDetailView';

export default function QuestionBankPage() {
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [keyword, setKeyword] = useState('');
  const [materials, setMaterials] = useState([]);
  const [materialVersionId, setMaterialVersionId] = useState('__all__');
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [rows, setRows] = useState([]);
  const [selectedRowKeys, setSelectedRowKeys] = useState([]);
  const [viewQuestionOpen, setViewQuestionOpen] = useState(false);
  const [viewQuestionRecord, setViewQuestionRecord] = useState(null);
  const [pagination, setPagination] = useState({ current: 1, pageSize: 20, total: 0 });
  const materialLabel = (m) => {
    const raw = String(m?.file_path || '').split('/').pop() || '';
    const name = raw.replace(/^v\d{8}_\d{6}_/, '') || raw || m?.material_version_id;
    return `${name}${m?.status === 'effective' ? '（当前生效）' : ''}`;
  };

  const loadData = async (page = pagination.current, pageSize = pagination.pageSize) => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const params = { page, page_size: pageSize, keyword };
      if (materialVersionId) params.material_version_id = materialVersionId;
      const res = await listBankQuestions(tenantId, params);
      setRows(res.items || []);
      setPagination({
        current: res.page || page,
        pageSize: res.page_size || pageSize,
        total: res.total || 0,
      });
      setSelectedRowKeys([]);
    } catch (e) {
      const apiMsg = e?.response?.data?.error?.message;
      const status = e?.response?.status;
      if (status === 404) {
        message.error('加载题库失败：后端接口不存在，请重启 admin_api.py 到最新版本');
      } else {
        message.error(apiMsg || `加载题库失败（HTTP ${status || 'unknown'}）`);
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  useEffect(() => {
    loadData(1, pagination.pageSize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, materialVersionId]);

  useEffect(() => {
    if (!tenantId) return;
    listMaterials(tenantId)
      .then((res) => setMaterials(res.items || []))
      .catch(() => setMaterials([]));
  }, [tenantId]);

  const onBatchDelete = async () => {
    if (!selectedRowKeys.length) {
      message.warning('请先勾选题目');
      return;
    }
    try {
      const res = await deleteBankQuestions(tenantId, { question_ids: selectedRowKeys });
      message.success(`已删除 ${res.deleted || 0} 题`);
      loadData(pagination.current, pagination.pageSize);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '删除失败');
    }
  };

  const onBatchExport = async () => {
    if (!selectedRowKeys.length) {
      message.warning('请先勾选题目');
      return;
    }
    setExporting(true);
    try {
      const blob = await exportBankQuestions(tenantId, { question_ids: selectedRowKeys });
      const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
      const filename = `${tenantId}_题库导出_${ts}.xlsx`;
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      window.URL.revokeObjectURL(url);
      message.success(`导出成功（${selectedRowKeys.length}题）`);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '导出失败');
    } finally {
      setExporting(false);
    }
  };

  const columns = [
    { title: 'ID', dataIndex: 'question_id', width: 90 },
    { title: '题干', dataIndex: '题干', ellipsis: true },
    { title: '答案', dataIndex: '正确答案', width: 100 },
    { title: '教材版本', dataIndex: '教材版本ID', width: 170, render: (v) => v || 'legacy' },
    { title: '来源路径', dataIndex: '来源路径', ellipsis: true, width: 320 },
    {
      title: '查看',
      dataIndex: '_view',
      width: 90,
      render: (_, record) => (
        <Button
          size="small"
          onClick={() => {
            setViewQuestionRecord(record);
            setViewQuestionOpen(true);
          }}
        >
          查看
        </Button>
      ),
    },
  ];

  return (
    <>
      <Space className="toolbar" wrap>
        <Select
          value={materialVersionId}
          style={{ width: 240 }}
          onChange={setMaterialVersionId}
          options={[
            { label: '全部教材', value: '__all__' },
            ...materials.map((m) => ({
              label: materialLabel(m),
              value: m.material_version_id,
            })),
          ]}
        />
        <Input
          placeholder="题干关键词"
          style={{ width: 260 }}
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
        />
        <Button type="primary" onClick={() => loadData(1, pagination.pageSize)}>查询</Button>
      </Space>

      <Card style={{ marginBottom: 12 }}>
        <Alert type="info" showIcon message={`已选 ${selectedRowKeys.length} 条，可批量导出或删除`} />
        <Space style={{ marginTop: 12 }}>
          <Button onClick={onBatchExport} loading={exporting} disabled={!selectedRowKeys.length}>批量导出</Button>
          <Popconfirm title="确认删除选中题目？" onConfirm={onBatchDelete}>
            <Button danger disabled={!selectedRowKeys.length}>批量删除</Button>
          </Popconfirm>
        </Space>
      </Card>

      <Card>
        <Table
          rowKey="question_id"
          loading={loading}
          columns={columns}
          dataSource={rows}
          rowSelection={{ selectedRowKeys, onChange: setSelectedRowKeys }}
          pagination={{
            current: pagination.current,
            pageSize: pagination.pageSize,
            total: pagination.total,
            showSizeChanger: true,
          }}
          onChange={(pg) => loadData(pg.current, pg.pageSize)}
        />
      </Card>
      <Modal
        title="题目详情"
        open={viewQuestionOpen}
        onCancel={() => {
          setViewQuestionOpen(false);
          setViewQuestionRecord(null);
        }}
        footer={null}
        width={900}
      >
        <QuestionDetailView question={viewQuestionRecord || {}} />
      </Modal>
    </>
  );
}
