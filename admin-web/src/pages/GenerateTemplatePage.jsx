import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Form, Input, InputNumber, Popconfirm, Select, Space, Table, Tag, Typography, message } from 'antd';
import {
  createGenerateTemplate,
  deleteGenerateTemplate,
  getSlicePathSummary,
  listGenerateTemplates,
  listMaterials,
  updateGenerateTemplate,
} from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const DEFAULT_MASTERY_RATIO = { 掌握: 6, 熟悉: 3, 了解: 1 };

const buildMaterialLabel = (item) => {
  const version = String(item?.material_version_id || '');
  const fileName = String(item?.file_path || '').split('/').pop() || '未命名教材';
  return `${version}｜${fileName}`;
};

const emptyRouteRule = () => ({
  rule_id: `local_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`,
  path_prefix: '',
  ratio: 0,
});

export default function GenerateTemplatePage() {
  const [form] = Form.useForm();
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [templates, setTemplates] = useState([]);
  const [materials, setMaterials] = useState([]);
  const [pathOptions, setPathOptions] = useState([]);
  const [editingId, setEditingId] = useState('');
  const [routeRules, setRouteRules] = useState([emptyRouteRule()]);
  const [deletingId, setDeletingId] = useState('');

  const loadTemplates = async (tid) => {
    const res = await listGenerateTemplates(tid);
    setTemplates(res?.items || []);
  };

  const loadMaterials = async (tid) => {
    const res = await listMaterials(tid);
    const items = (res?.items || []).filter((item) => String(item?.status || '') === 'effective');
    setMaterials(items);
    return items;
  };

  const loadPathOptions = async (tid, materialVersionId) => {
    if (!tid || !materialVersionId) {
      setPathOptions([]);
      return;
    }
    const [level2, level3] = await Promise.all([
      getSlicePathSummary(tid, { material_version_id: materialVersionId, level: 2 }),
      getSlicePathSummary(tid, { material_version_id: materialVersionId, level: 3 }),
    ]);
    const seen = new Map();
    [...(level2?.items || []), ...(level3?.items || [])].forEach((item) => {
      const pathPrefix = String(item?.path_prefix || '').trim();
      if (!pathPrefix) return;
      if (!seen.has(pathPrefix)) {
        seen.set(pathPrefix, {
          label: `${pathPrefix}（approved ${Number(item?.approved || 0)} / total ${Number(item?.total || 0)}）`,
          value: pathPrefix,
        });
      }
    });
    setPathOptions(Array.from(seen.values()));
  };

  const resetEditor = (nextMaterialId = '') => {
    setEditingId('');
    setRouteRules([emptyRouteRule()]);
    form.setFieldsValue({
      name: '',
      description: '',
      material_version_id: nextMaterialId || undefined,
      question_count: 10,
      mastery_master: DEFAULT_MASTERY_RATIO.掌握,
      mastery_familiar: DEFAULT_MASTERY_RATIO.熟悉,
      mastery_understand: DEFAULT_MASTERY_RATIO.了解,
    });
  };

  const loadPage = async (tid) => {
    if (!tid) return;
    setLoading(true);
    try {
      const materialItems = await loadMaterials(tid);
      await loadTemplates(tid);
      const defaultMaterialId = String((materialItems[0] || {}).material_version_id || '');
      resetEditor(defaultMaterialId);
      if (defaultMaterialId) {
        await loadPathOptions(tid, defaultMaterialId);
      } else {
        setPathOptions([]);
      }
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '加载出题模板页面失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  useEffect(() => {
    loadPage(tenantId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  const selectedTemplate = useMemo(
    () => templates.find((item) => String(item?.template_id || '') === editingId) || null,
    [templates, editingId],
  );

  const routeRatioTotal = useMemo(
    () => routeRules.reduce((sum, item) => sum + Number(item?.ratio || 0), 0),
    [routeRules],
  );

  const masteryRatioTotal = Form.useWatch('mastery_master', form) || 0;
  const masteryFamiliar = Form.useWatch('mastery_familiar', form) || 0;
  const masteryUnderstand = Form.useWatch('mastery_understand', form) || 0;
  const masteryTotal = Number(masteryRatioTotal || 0) + Number(masteryFamiliar || 0) + Number(masteryUnderstand || 0);

  const onMaterialChange = async (materialVersionId) => {
    form.setFieldValue('material_version_id', materialVersionId);
    setRouteRules([emptyRouteRule()]);
    try {
      await loadPathOptions(tenantId, materialVersionId);
    } catch (e) {
      setPathOptions([]);
      message.error(e?.response?.data?.error?.message || '加载切片路由失败');
    }
  };

  const onEdit = async (record) => {
    setEditingId(String(record?.template_id || ''));
    setRouteRules((record?.route_rules || []).length ? record.route_rules : [emptyRouteRule()]);
    form.setFieldsValue({
      name: record?.name || '',
      description: record?.description || '',
      material_version_id: record?.material_version_id || undefined,
      question_count: Number(record?.question_count || 10),
      mastery_master: Number(record?.mastery_ratio?.掌握 || DEFAULT_MASTERY_RATIO.掌握),
      mastery_familiar: Number(record?.mastery_ratio?.熟悉 || DEFAULT_MASTERY_RATIO.熟悉),
      mastery_understand: Number(record?.mastery_ratio?.了解 || DEFAULT_MASTERY_RATIO.了解),
    });
    try {
      await loadPathOptions(tenantId, record?.material_version_id || '');
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '加载模板路由失败');
    }
  };

  const onSubmit = async (values) => {
    if (!tenantId) return;
    const validRouteRules = routeRules
      .map((item) => ({
        rule_id: item.rule_id,
        path_prefix: String(item.path_prefix || '').trim(),
        ratio: Number(item.ratio || 0),
      }))
      .filter((item) => item.path_prefix && item.ratio > 0);
    if (!validRouteRules.length) {
      message.warning('至少需要配置一条切片路由占比');
      return;
    }
    const payload = {
      name: String(values?.name || '').trim(),
      description: String(values?.description || '').trim(),
      material_version_id: values?.material_version_id,
      question_count: Number(values?.question_count || 10),
      mastery_ratio: {
        掌握: Number(values?.mastery_master || 0),
        熟悉: Number(values?.mastery_familiar || 0),
        了解: Number(values?.mastery_understand || 0),
      },
      route_rules: validRouteRules,
    };
    setSaving(true);
    try {
      if (editingId) {
        await updateGenerateTemplate(tenantId, editingId, payload);
        message.success('出题模板已更新');
      } else {
        const res = await createGenerateTemplate(tenantId, payload);
        setEditingId(String(res?.item?.template_id || ''));
        message.success('出题模板已创建');
      }
      await loadTemplates(tenantId);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '保存模板失败');
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (templateId) => {
    if (!tenantId || !templateId) return;
    setDeletingId(templateId);
    try {
      await deleteGenerateTemplate(tenantId, templateId);
      if (editingId === templateId) {
        resetEditor(String((materials[0] || {}).material_version_id || ''));
      }
      await loadTemplates(tenantId);
      message.success('出题模板已删除');
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '删除模板失败');
    } finally {
      setDeletingId('');
    }
  };

  const columns = [
    {
      title: '模板名称',
      dataIndex: 'name',
      render: (_, record) => (
        <Button type="link" style={{ padding: 0 }} onClick={() => onEdit(record)}>
          {record?.name || '-'}
        </Button>
      ),
    },
    {
      title: '教材',
      dataIndex: 'material_version_id',
      render: (value) => value || '-',
    },
    {
      title: '题量',
      dataIndex: 'question_count',
      width: 80,
    },
    {
      title: '掌握比',
      width: 180,
      render: (_, record) => `掌握:${record?.mastery_ratio?.掌握 || 0} / 熟悉:${record?.mastery_ratio?.熟悉 || 0} / 了解:${record?.mastery_ratio?.了解 || 0}`,
    },
    {
      title: '路由数',
      width: 90,
      render: (_, record) => Number(record?.route_rules?.length || 0),
    },
    {
      title: '操作',
      width: 140,
      render: (_, record) => (
        <Space>
          <Button size="small" onClick={() => onEdit(record)}>编辑</Button>
          <Popconfirm title="删除这个出题模板？" onConfirm={() => onDelete(String(record?.template_id || ''))}>
            <Button size="small" danger loading={deletingId === String(record?.template_id || '')}>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Alert
        type="info"
        showIcon
        message="出题要求模板"
        description="模板会固定教材、题量、切片路由占比，以及掌握/熟悉/了解的整体占比。AI 出题里选择模板后，会按模板严格分配切片。"
      />

      <Card
        title="模板列表"
        extra={(
          <Space>
            <Button onClick={() => loadPage(tenantId)} loading={loading}>刷新</Button>
            <Button type="primary" onClick={() => resetEditor(String((materials[0] || {}).material_version_id || ''))}>新建模板</Button>
          </Space>
        )}
      >
        <Table
          rowKey={(record) => String(record?.template_id || '')}
          loading={loading}
          columns={columns}
          dataSource={templates}
          pagination={{ pageSize: 6, showSizeChanger: false }}
          locale={{ emptyText: '当前城市还没有出题模板' }}
        />
      </Card>

      <Card title={editingId ? '编辑模板' : '新建模板'}>
        <Form form={form} layout="vertical" onFinish={onSubmit}>
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            <Space wrap style={{ width: '100%' }} align="start">
              <Form.Item
                name="material_version_id"
                label="教材版本"
                rules={[{ required: true, message: '请选择教材版本' }]}
                style={{ minWidth: 420 }}
              >
                <Select
                  placeholder="选择教材版本"
                  options={materials.map((item) => ({ label: buildMaterialLabel(item), value: item.material_version_id }))}
                  onChange={onMaterialChange}
                />
              </Form.Item>
              <Form.Item
                name="name"
                label="模板名称"
                rules={[{ required: true, whitespace: true, message: '请输入模板名称' }]}
                style={{ minWidth: 280 }}
              >
                <Input placeholder="例如：上册业务技能A卷模板" />
              </Form.Item>
              <Form.Item name="question_count" label="总题量" initialValue={10}>
                <InputNumber min={1} max={200} style={{ width: 120 }} />
              </Form.Item>
            </Space>

            <Form.Item name="description" label="说明">
              <Input.TextArea rows={2} placeholder="可选：记录这个模板适用于哪套试卷/考试场景" />
            </Form.Item>

            <Card size="small" title="掌握程度占比">
              <Space wrap>
                <Form.Item name="mastery_master" label="掌握" initialValue={6}>
                  <InputNumber min={0} precision={0} style={{ width: 120 }} />
                </Form.Item>
                <Form.Item name="mastery_familiar" label="熟悉" initialValue={3}>
                  <InputNumber min={0} precision={0} style={{ width: 120 }} />
                </Form.Item>
                <Form.Item name="mastery_understand" label="了解" initialValue={1}>
                  <InputNumber min={0} precision={0} style={{ width: 120 }} />
                </Form.Item>
                <Tag color={masteryTotal > 0 ? 'blue' : 'red'}>
                  当前比例：{Number(masteryRatioTotal || 0)} : {Number(masteryFamiliar || 0)} : {Number(masteryUnderstand || 0)}
                </Tag>
              </Space>
            </Card>

            <Card
              size="small"
              title="切片路由占比"
              extra={<Tag color={routeRatioTotal > 0 ? 'blue' : 'red'}>当前合计 {routeRatioTotal}</Tag>}
            >
              <Space direction="vertical" style={{ width: '100%' }} size={10}>
                {routeRules.map((item, index) => (
                  <Space key={item.rule_id} wrap style={{ width: '100%' }} align="start">
                    <Select
                      showSearch
                      optionFilterProp="label"
                      style={{ minWidth: 520 }}
                      placeholder="选择切片路由前缀"
                      value={item.path_prefix || undefined}
                      options={pathOptions}
                      onChange={(value) => {
                        setRouteRules((prev) => prev.map((rule, idx) => (idx === index ? { ...rule, path_prefix: value } : rule)));
                      }}
                    />
                    <InputNumber
                      min={0}
                      precision={2}
                      style={{ width: 140 }}
                      value={Number(item.ratio || 0)}
                      addonAfter="%"
                      onChange={(value) => {
                        setRouteRules((prev) => prev.map((rule, idx) => (idx === index ? { ...rule, ratio: Number(value || 0) } : rule)));
                      }}
                    />
                    <Button
                      danger
                      disabled={routeRules.length === 1}
                      onClick={() => setRouteRules((prev) => prev.filter((_, idx) => idx !== index))}
                    >
                      删除
                    </Button>
                  </Space>
                ))}
                <Space>
                  <Button onClick={() => setRouteRules((prev) => [...prev, emptyRouteRule()])}>新增路由</Button>
                  <Typography.Text type="secondary">
                    路由占比不强制等于 100，系统会按你填的权重归一化分配题量。
                  </Typography.Text>
                </Space>
              </Space>
            </Card>

            {selectedTemplate ? (
              <Alert
                type="success"
                showIcon
                message={`当前正在编辑：${selectedTemplate.name}`}
                description={`模板ID：${selectedTemplate.template_id}，最近更新时间：${selectedTemplate.updated_at || '-'}`}
              />
            ) : null}

            <Space>
              <Button type="primary" htmlType="submit" loading={saving}>保存模板</Button>
              <Button onClick={() => resetEditor(String((materials[0] || {}).material_version_id || ''))}>清空</Button>
            </Space>
          </Space>
        </Form>
      </Card>
    </Space>
  );
}
