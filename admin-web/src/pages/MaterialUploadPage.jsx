import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Dropdown, Input, List, message, Modal, Popconfirm, Row, Col, Select, Space, Tabs, Typography, Upload } from 'antd';
import { InboxOutlined, MoreOutlined } from '@ant-design/icons';
import {
  archiveMaterial,
  deleteMaterial,
  listMaterials,
  remapMaterial,
  resliceMaterial,
  setMaterialEffective,
  uploadMaterial,
  uploadReferenceAndMap,
} from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const LONG_TASK_TIMEOUT_MS = 90 * 60 * 1000;

export default function MaterialUploadPage() {
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [fileList, setFileList] = useState([]);
  const [textContent, setTextContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [materials, setMaterials] = useState([]);
  const [lastResult, setLastResult] = useState(null);
  const [referenceFileList, setReferenceFileList] = useState([]);
  const [selectedMaterialIdForMap, setSelectedMaterialIdForMap] = useState('');
  const [mappingLoading, setMappingLoading] = useState(false);
  const [resliceLoadingId, setResliceLoadingId] = useState('');
  const [remapLoadingId, setRemapLoadingId] = useState('');

  const materialFileName = (item) => {
    const raw = String(item?.file_path || '').split('/').pop() || '';
    return raw.replace(/^v\d{8}_\d{6}_/, '') || raw || String(item?.material_version_id || '');
  };
  const materialStatusLabel = (status) => {
    if (status === 'slicing') return '生成中';
    if (status === 'effective') return '生效';
    if (status === 'archived') return '已下线';
    if (status === 'failed') return '失败';
    return '未知';
  };
  const flowStatusLabel = (status) => {
    if (status === 'running') return '生成中';
    if (status === 'success') return '成功';
    if (status === 'failed') return '失败';
    return '待生成';
  };
  const flowStatusClass = (status) => {
    if (status === 'success') return 'status-text status-ok';
    if (status === 'failed') return 'status-text status-warn';
    if (status === 'running') return 'status-text status-warn';
    return 'status-text status-muted';
  };
  const canSetEffective = (item) => {
    const backendFlag = item?.can_set_effective;
    if (typeof backendFlag === 'boolean') return backendFlag;
    // Backward compatibility for old backend payloads.
    return String(item?.slice_status || '') === 'success' && String(item?.mapping_status || '') === 'success';
  };

  const loadMaterials = async (tid) => {
    if (!tid) return;
    try {
      const res = await listMaterials(tid);
      const items = res.items || [];
      setMaterials(items);
      setSelectedMaterialIdForMap((prev) => prev || (items[0]?.material_version_id || ''));
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '加载教材版本失败');
    }
  };

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  useEffect(() => {
    if (!tenantId) return;
    setSelectedMaterialIdForMap('');
    loadMaterials(tenantId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  useEffect(() => {
    if (!tenantId) return undefined;
    const hasRunning = materials.some((x) => x?.slice_status === 'running' || x?.mapping_status === 'running' || x?.status === 'slicing');
    if (!hasRunning) return undefined;
    const timer = setInterval(() => {
      loadMaterials(tenantId);
    }, 3000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, materials]);

  const onSubmit = async () => {
    if (!tenantId) return;
    const file = fileList[0]?.originFileObj;
    if (!file && !textContent.trim()) {
      message.warning('请上传文件或输入教材文字');
      return;
    }
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const localVersionId = `v${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    const pendingName = file?.name || 'manual.txt';
    const pendingItem = {
      material_version_id: `pending_${Date.now()}`,
      file_path: pendingName,
      status: 'slicing',
      reference_file: '',
      mapping_ready: false,
      _isPending: true,
      _pendingVersionHint: localVersionId,
    };
    setMaterials((prev) => [pendingItem, ...prev]);
    setLoading(true);
    setLastResult(null);
    try {
      const res = await uploadMaterial(
        tenantId,
        { file, text: textContent },
        {
          timeout: LONG_TASK_TIMEOUT_MS,
        }
      );
      setLastResult(res);
      message.success(`上传并切片成功，共生成 ${res.slice_count || 0} 条`);
      setFileList([]);
      setTextContent('');
      await loadMaterials(tenantId);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '上传切片失败');
    } finally {
      setMaterials((prev) => prev.filter((x) => !x?._isPending));
      setLoading(false);
    }
  };

  const onSetEffective = async (materialVersionId) => {
    try {
      await setMaterialEffective(tenantId, materialVersionId);
      message.success('已切换为生效教材');
      await loadMaterials(tenantId);
    } catch (e) {
      const status = e?.response?.status;
      const msg = e?.response?.data?.error?.message;
      message.error(msg || (status === 404 ? '后端未升级（缺少教材版本管理接口），请重启 admin_api.py' : '设置生效失败'));
    }
  };

  const onUploadReferenceAndMap = async () => {
    if (!tenantId) return;
    if (!selectedMaterialIdForMap) {
      message.warning('请先选择教材版本');
      return;
    }
    const file = referenceFileList[0]?.originFileObj;
    if (!file) {
      message.warning('请上传参考题表格（xlsx/xls）');
      return;
    }
    setMaterials((prev) => prev.map((x) => (
      x.material_version_id === selectedMaterialIdForMap
        ? { ...x, mapping_status: 'running', mapping_error: '' }
        : x
    )));
    setMappingLoading(true);
    try {
      const res = await uploadReferenceAndMap(tenantId, selectedMaterialIdForMap, file, { timeout: LONG_TASK_TIMEOUT_MS });
      message.success(`参考题上传并生成映射成功，映射 ${res.mapping_total || 0} 条`);
      setReferenceFileList([]);
      await loadMaterials(tenantId);
    } catch (e) {
      if (e?.code === 'ECONNABORTED') {
        message.warning('上传/映射任务超时（任务较重），后端可能仍在执行，请稍后刷新列表确认结果');
      } else {
        message.error(e?.response?.data?.error?.message || '参考题上传/映射失败');
      }
    } finally {
      setMappingLoading(false);
    }
  };

  const onArchive = async (materialVersionId) => {
    try {
      await archiveMaterial(tenantId, materialVersionId);
      message.success('教材已下线');
      await loadMaterials(tenantId);
    } catch (e) {
      const status = e?.response?.status;
      const msg = e?.response?.data?.error?.message;
      message.error(msg || (status === 404 ? '后端未升级（缺少教材版本管理接口），请重启 admin_api.py' : '下线失败'));
    }
  };

  const onReslice = async (materialVersionId) => {
    if (!tenantId) return;
    setResliceLoadingId(materialVersionId);
    setMaterials((prev) => prev.map((x) => (
      x.material_version_id === materialVersionId
        ? { ...x, status: 'slicing', slice_status: 'running', slice_error: '' }
        : x
    )));
    try {
      const res = await resliceMaterial(tenantId, materialVersionId);
      message.success(`重新切片成功，共 ${res.slice_count || 0} 条`);
      await loadMaterials(tenantId);
    } catch (e) {
      if (e?.code === 'ECONNABORTED') {
        message.error('重新切片超时（任务较重），请稍后刷新列表确认结果');
      } else {
        message.error(e?.response?.data?.error?.message || '重新切片失败');
      }
    } finally {
      setResliceLoadingId('');
    }
  };

  const onRemap = async (materialVersionId) => {
    if (!tenantId) return;
    setRemapLoadingId(materialVersionId);
    setMaterials((prev) => prev.map((x) => (
      x.material_version_id === materialVersionId
        ? { ...x, mapping_status: 'running', mapping_error: '' }
        : x
    )));
    try {
      const res = await remapMaterial(tenantId, materialVersionId);
      message.success(`重新映射成功，共 ${res.mapping_total || 0} 条`);
      await loadMaterials(tenantId);
    } catch (e) {
      if (e?.code === 'ECONNABORTED') {
        message.error('重新映射超时（任务较重），请稍后刷新列表确认结果');
      } else {
        message.error(e?.response?.data?.error?.message || '重新映射失败');
      }
    } finally {
      setRemapLoadingId('');
    }
  };

  const onDelete = async (materialVersionId, force = false) => {
    try {
      await deleteMaterial(tenantId, materialVersionId, force);
      message.success(force ? '教材已强制删除' : '教材已删除');
      await loadMaterials(tenantId);
    } catch (e) {
      const status = e?.response?.status;
      const msg = e?.response?.data?.error?.message;
      if (!force && (status === 409 || String(msg || '').includes('关联') || String(msg || '').toLowerCase().includes('in use'))) {
        Modal.confirm({
          title: '普通删除失败',
          content: '该教材版本存在关联数据（切片/映射/题库等）。是否继续强制删除？',
          okText: '强制删除',
          okButtonProps: { danger: true },
          cancelText: '取消',
          onOk: () => onDelete(materialVersionId, true),
        });
        return;
      }
      message.error(msg || (status === 404 ? '后端未升级（缺少教材版本管理接口），请重启 admin_api.py' : '删除失败'));
    }
  };

  return (
    <>
      <Alert
        type="info"
        showIcon
        message="先上传教材（docx/文本）生成切片；再上传该教材对应参考题表格生成映射。"
        style={{ marginBottom: 12 }}
      />
      <Row gutter={12} align="top">
        <Col xs={24} lg={6}>
          <Card title="城市教材版本" style={{ height: 'calc(100vh - 190px)' }} bodyStyle={{ height: 'calc(100vh - 250px)', overflow: 'auto' }}>
            <List
              dataSource={materials}
              rowKey={(x) => x.material_version_id}
              renderItem={(item) => (
                <List.Item>
                  {(() => {
                    const sliceStatus = item.slice_status || (item.status === 'slicing' ? 'running' : (item.slice_ready ? 'success' : 'pending'));
                    const mappingStatus = item.mapping_status || (item.mapping_ready ? 'success' : 'pending');
                    return (
                  <Space direction="vertical" size={0} style={{ width: '100%' }}>
                    <div className="material-item-head">
                      <Typography.Text strong>{materialFileName(item)}</Typography.Text>
                    </div>
                    <Typography.Text type="secondary">版本：{item._pendingVersionHint || item.material_version_id}</Typography.Text>
                    <Typography.Text type="secondary">{item.file_path || '-'}</Typography.Text>
                    <Typography.Text type="secondary">
                      参考题：
                      <span className={item.reference_file ? 'status-text status-ok' : 'status-text status-muted'}>
                        {item.reference_file ? '已上传' : '未上传'}
                      </span>
                      映射：
                      <span className={flowStatusClass(mappingStatus)}>
                        {flowStatusLabel(mappingStatus)}
                      </span>
                    </Typography.Text>
                    {item.mapping_error ? (
                      <Typography.Text type="danger">映射错误：{item.mapping_error}</Typography.Text>
                    ) : null}
                    <Typography.Text type="secondary">
                      切片状态：
                      <span className={flowStatusClass(sliceStatus)}>
                        {flowStatusLabel(sliceStatus)}
                      </span>
                    </Typography.Text>
                    {item.slice_error ? (
                      <Typography.Text type="danger">切片错误：{item.slice_error}</Typography.Text>
                    ) : null}
                    <Typography.Text type="secondary">
                      教材版本状态：
                      <span className={`status-text ${item.status === 'effective' ? 'status-ok' : item.status === 'archived' ? 'status-muted' : 'status-warn'}`}>
                        {materialStatusLabel(item.status)}
                      </span>
                    </Typography.Text>
                    <div className="material-item-actions">
                      <Space size={6} wrap>
                        {item._isPending ? null : (
                          <>
                        {item.status !== 'effective' && (
                          <Button
                            key="effective"
                            size="small"
                            disabled={!canSetEffective(item)}
                            title={canSetEffective(item) ? '' : (item?.effective_block_reason || '需至少存在1条映射核对与切片核对都完成的知识切片')}
                            onClick={() => onSetEffective(item.material_version_id)}
                          >
                            生效
                          </Button>
                        )}
                        <Button
                          key="reslice"
                          size="small"
                          loading={resliceLoadingId === item.material_version_id}
                          onClick={() => onReslice(item.material_version_id)}
                        >
                          重新切片
                        </Button>
                        {item.mapping_ready && (
                          <Button
                            key="remap"
                            size="small"
                            loading={remapLoadingId === item.material_version_id}
                            onClick={() => onRemap(item.material_version_id)}
                          >
                            重新映射
                          </Button>
                        )}
                        <Dropdown
                          trigger={['click']}
                          menu={{
                            items: [
                              ...(item.status === 'effective' ? [{ key: 'archive', label: '下线' }] : []),
                              ...(item.status === 'effective' ? [{ type: 'divider' }] : []),
                              { key: 'delete', label: '删除', danger: true },
                            ],
                            onClick: ({ key }) => {
                              if (key === 'archive') {
                                Modal.confirm({
                                  title: '确认下线该教材版本？',
                                  okText: '下线',
                                  cancelText: '取消',
                                  onOk: () => onArchive(item.material_version_id),
                                });
                                return;
                              }
                              if (key === 'delete') {
                                Modal.confirm({
                                  title: '确认删除该教材版本？',
                                  okText: '删除',
                                  okButtonProps: { danger: true },
                                  cancelText: '取消',
                                  onOk: () => onDelete(item.material_version_id, false),
                                });
                              }
                            },
                          }}
                        >
                          <Button size="small" icon={<MoreOutlined />} />
                        </Dropdown>
                          </>
                        )}
                      </Space>
                    </div>
                  </Space>
                    );
                  })()}
                </List.Item>
              )}
            />
          </Card>
        </Col>

        <Col xs={24} lg={18}>
          <Card title="上传内容" style={{ height: 'calc(100vh - 190px)' }} bodyStyle={{ height: 'calc(100vh - 250px)', overflow: 'auto' }}>
            <Tabs
              defaultActiveKey="slice"
              items={[
                {
                  key: 'slice',
                  label: '教材（生成切片）',
                  children: (
                    <Space direction="vertical" style={{ width: '100%' }} size={12}>
                      <Upload.Dragger
                        multiple={false}
                        fileList={fileList}
                        beforeUpload={() => false}
                        onChange={({ fileList: next }) => setFileList(next.slice(-1))}
                        accept=".docx,.txt,.md"
                      >
                        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
                        <p className="ant-upload-text">拖拽文件到这里，或点击上传</p>
                      </Upload.Dragger>
                      <Input.TextArea
                        rows={5}
                        placeholder="或在此粘贴教材文字内容（支持纯文本）"
                        value={textContent}
                        onChange={(e) => setTextContent(e.target.value)}
                      />
                      <Button type="primary" loading={loading} onClick={onSubmit}>
                        上传并生成切片
                      </Button>
                      {lastResult && (
                        <Alert
                          type="success"
                          showIcon
                          message={`最近一次结果：版本 ${lastResult.material_version_id}，生成切片 ${lastResult.slice_count || 0} 条`}
                        />
                      )}
                    </Space>
                  ),
                },
                {
                  key: 'mapping',
                  label: '参考题库（生成映射）',
                  children: (
                    <Space direction="vertical" style={{ width: '100%' }} size={12}>
                      <Select
                        value={selectedMaterialIdForMap}
                        style={{ width: 420 }}
                        placeholder="选择要生成映射的教材版本"
                        onChange={setSelectedMaterialIdForMap}
                        options={materials.map((m) => ({
                          label: `${materialFileName(m)} [${m.material_version_id}]`,
                          value: m.material_version_id,
                        }))}
                      />
                      <Upload.Dragger
                        multiple={false}
                        fileList={referenceFileList}
                        beforeUpload={() => false}
                        onChange={({ fileList: next }) => setReferenceFileList(next.slice(-1))}
                        accept=".xlsx,.xls"
                      >
                        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
                        <p className="ant-upload-text">拖拽参考题库表格到这里，或点击上传</p>
                      </Upload.Dragger>
                      <Button type="primary" loading={mappingLoading} onClick={onUploadReferenceAndMap}>
                        上传参考题并生成映射
                      </Button>
                    </Space>
                  ),
                },
              ]}
            />
          </Card>
        </Col>
      </Row>
    </>
  );
}
