import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Form, Input, message, Modal, Popconfirm, Select, Space, Table, Tag } from 'antd';
import {
  batchBindAdminUsers,
  deleteAdminCity,
  deleteAdminUser,
  listAdminCities,
  listAdminUsers,
  updateAdminCityStatus,
  upsertAdminCity,
  upsertAdminUser,
} from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const ROLE_OPTIONS = [
  { label: 'platform_admin', value: 'platform_admin' },
  { label: 'city_admin', value: 'city_admin' },
  { label: 'city_teacher', value: 'city_teacher' },
  { label: 'city_viewer', value: 'city_viewer' },
];

export default function CityAdminPage() {
  const [globalTenantId, setGlobalTenantId] = useState(getGlobalTenantId());
  const [cities, setCities] = useState([]);
  const [cityCatalog, setCityCatalog] = useState([]);
  const [users, setUsers] = useState([]);
  const [cityForm] = Form.useForm();
  const [userForm] = Form.useForm();
  const [batchForm] = Form.useForm();
  const [loadingCities, setLoadingCities] = useState(false);
  const [loadingUsers, setLoadingUsers] = useState(false);
  const [citySaving, setCitySaving] = useState(false);
  const [userSaving, setUserSaving] = useState(false);
  const [queryCity, setQueryCity] = useState({ q: '', status: 'all', page: 1, page_size: 10 });
  const [queryUser, setQueryUser] = useState({ q: '', role: 'all', page: 1, page_size: 10 });
  const [cityTotal, setCityTotal] = useState(0);
  const [userTotal, setUserTotal] = useState(0);
  const [editingCityId, setEditingCityId] = useState(null);
  const [editingUserId, setEditingUserId] = useState(null);
  const [cityModalOpen, setCityModalOpen] = useState(false);
  const [userModalOpen, setUserModalOpen] = useState(false);
  const [selectedUserRowKeys, setSelectedUserRowKeys] = useState([]);

  useEffect(() => subscribeGlobalTenant((tid) => setGlobalTenantId(tid)), []);

  useEffect(() => {
    if (!globalTenantId) return;
    setQueryCity((s) => ({ ...s, q: globalTenantId, page: 1 }));
    setQueryUser((s) => ({ ...s, q: globalTenantId, page: 1 }));
  }, [globalTenantId]);

  const loadCities = async () => {
    setLoadingCities(true);
    try {
      const res = await listAdminCities(queryCity);
      setCities(res.items || []);
      setCityTotal(res.total || 0);
    } catch (e) {
      const status = e?.response?.status;
      const apiMsg = e?.response?.data?.error?.message;
      message.error(apiMsg || `加载城市列表失败（HTTP ${status || 'unknown'}）`);
    } finally {
      setLoadingCities(false);
    }
  };

  const loadCityCatalog = async () => {
    try {
      const res = await listAdminCities({ page: 1, page_size: 200, status: 'all', q: '' });
      setCityCatalog(res.items || []);
    } catch (e) {
      // city list toast is handled by loadCities; keep silent here.
    }
  };

  const loadUsers = async () => {
    setLoadingUsers(true);
    try {
      const res = await listAdminUsers(queryUser);
      setUsers(res.items || []);
      setUserTotal(res.total || 0);
    } catch (e) {
      const status = e?.response?.status;
      const apiMsg = e?.response?.data?.error?.message;
      message.error(apiMsg || `加载系统号列表失败（HTTP ${status || 'unknown'}）`);
    } finally {
      setLoadingUsers(false);
    }
  };

  useEffect(() => {
    loadCities();
    loadCityCatalog();
  }, [queryCity.page, queryCity.page_size, queryCity.q, queryCity.status]);

  useEffect(() => {
    loadUsers();
  }, [queryUser.page, queryUser.page_size, queryUser.q, queryUser.role]);

  const onCreateCity = async (values) => {
    setCitySaving(true);
    try {
      await upsertAdminCity(values);
      message.success('城市保存成功');
      setEditingCityId(null);
      setCityModalOpen(false);
      cityForm.resetFields();
      setQueryCity((s) => ({ ...s, page: 1 }));
      loadCities();
      loadCityCatalog();
      loadUsers();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '保存城市失败');
    } finally {
      setCitySaving(false);
    }
  };

  const onCreateUser = async (values) => {
    setUserSaving(true);
    try {
      await upsertAdminUser({
        system_user: values.system_user,
        role: values.role,
        tenants: values.tenants || [],
      });
      message.success('系统号权限保存成功');
      setEditingUserId(null);
      setUserModalOpen(false);
      userForm.resetFields();
      setQueryUser((s) => ({ ...s, page: 1 }));
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '保存系统号权限失败');
    } finally {
      setUserSaving(false);
    }
  };

  const onEditCity = (row) => {
    setEditingCityId(row.tenant_id);
    cityForm.setFieldsValue({
      tenant_id: row.tenant_id,
      name: row.name,
      is_active: row.is_active !== false,
    });
    setCityModalOpen(true);
  };

  const onDeleteCity = async (tenantId, force = false) => {
    try {
      await deleteAdminCity(tenantId, force);
      message.success(force ? '城市已强制删除' : '城市删除成功');
      // If already on page 1, pagination state may not trigger effect; force refresh explicitly.
      loadCities();
      loadCityCatalog();
      loadUsers();
      setSelectedUserRowKeys([]);
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '删除城市失败');
    }
  };

  const onToggleCityStatus = async (row) => {
    try {
      await updateAdminCityStatus(row.tenant_id, { is_active: !row.is_active });
      message.success('城市状态已更新');
      loadCities();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '更新城市状态失败');
    }
  };

  const onEditUser = (row) => {
    setEditingUserId(row.system_user);
    userForm.setFieldsValue({
      system_user: row.system_user,
      role: row.role,
      tenants: row.tenants || [],
    });
    setUserModalOpen(true);
  };

  const onDeleteUser = async (system_user) => {
    try {
      await deleteAdminUser({ system_user });
      message.success('系统号已删除');
      setQueryUser((s) => ({ ...s, page: 1 }));
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '删除系统号失败');
    }
  };

  const onBatchBind = async (values) => {
    try {
      const res = await batchBindAdminUsers({
        system_users: selectedUserRowKeys,
        tenants: values.tenants || [],
        op: values.op || 'add',
      });
      message.success(`批量处理完成：成功 ${res.affected || 0} 个，跳过 ${(res.skipped || []).length} 个`);
      batchForm.resetFields();
      loadUsers();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '批量处理失败');
    }
  };

  const cityOptions = useMemo(
    () => cityCatalog
      .filter((c) => c.is_active !== false)
      .map((c) => ({ label: `${c.name} (${c.tenant_id})`, value: c.tenant_id })),
    [cityCatalog]
  );

  const visibleUsers = useMemo(() => {
    if (!globalTenantId) return users;
    return users.filter((u) => Array.isArray(u.tenants) && u.tenants.includes(globalTenantId));
  }, [users, globalTenantId]);

  return (
    <>
      <Card style={{ marginBottom: 12 }}>
        <Alert type="info" showIcon message={`城市管理仅平台管理员可见：新增/改名城市，以及系统号绑定城市权限。当前全局城市：${globalTenantId || '未选择'}`} />
      </Card>

      <Card title="新增/编辑城市" style={{ marginBottom: 12 }}>
        <Space wrap style={{ marginBottom: 12 }}>
          <Input
            placeholder="搜索城市ID/名称"
            style={{ width: 220 }}
            value={queryCity.q}
            onChange={(e) => setQueryCity((s) => ({ ...s, q: e.target.value, page: 1 }))}
            onPressEnter={() => setQueryCity((s) => ({ ...s, page: 1 }))}
          />
          <Select
            style={{ width: 140 }}
            value={queryCity.status}
            onChange={(v) => setQueryCity((s) => ({ ...s, status: v, page: 1 }))}
            options={[
              { label: '全部状态', value: 'all' },
              { label: '启用', value: 'active' },
              { label: '停用', value: 'inactive' },
            ]}
          />
          <Button type="primary" onClick={() => setQueryCity((s) => ({ ...s, page: 1 }))}>查询</Button>
          <Button onClick={() => setQueryCity({ q: '', status: 'all', page: 1, page_size: 10 })}>重置</Button>
          <Button
            type="primary"
            onClick={() => {
              setEditingCityId(null);
              cityForm.resetFields();
              cityForm.setFieldValue('is_active', true);
              setCityModalOpen(true);
            }}
          >
            新增城市
          </Button>
        </Space>
        <Table
          style={{ marginTop: 12 }}
          rowKey="tenant_id"
          loading={loadingCities}
          columns={[
            { title: '城市ID', dataIndex: 'tenant_id', width: 140 },
            { title: '城市名称', dataIndex: 'name' },
            {
              title: '状态',
              dataIndex: 'is_active',
              width: 120,
              render: (v) => (v === false ? <Tag color="default">停用</Tag> : <Tag color="success">启用</Tag>),
            },
            {
              title: '操作',
              width: 260,
              render: (_, row) => (
                <Space>
                  <Button size="small" onClick={() => onEditCity(row)}>编辑</Button>
                  <Button size="small" onClick={() => onToggleCityStatus(row)}>
                    {row.is_active === false ? '启用' : '停用'}
                  </Button>
                  <Popconfirm title="确认删除该城市？" onConfirm={() => onDeleteCity(row.tenant_id, false)}>
                    <Button size="small" danger>删除</Button>
                  </Popconfirm>
                  <Popconfirm title="强制删除会移除关联权限，确认继续？" onConfirm={() => onDeleteCity(row.tenant_id, true)}>
                    <Button size="small" danger type="dashed">强制删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
          dataSource={cities}
          pagination={{
            current: queryCity.page,
            pageSize: queryCity.page_size,
            total: cityTotal,
            onChange: (page, pageSize) => setQueryCity((s) => ({ ...s, page, page_size: pageSize })),
          }}
        />
      </Card>

      <Card title="系统号城市权限">
        <Space wrap style={{ marginBottom: 12 }}>
          <Input
            placeholder="搜索系统号"
            style={{ width: 220 }}
            value={queryUser.q}
            onChange={(e) => setQueryUser((s) => ({ ...s, q: e.target.value, page: 1 }))}
            onPressEnter={() => setQueryUser((s) => ({ ...s, page: 1 }))}
          />
          <Select
            style={{ width: 160 }}
            value={queryUser.role}
            onChange={(v) => setQueryUser((s) => ({ ...s, role: v, page: 1 }))}
            options={[{ label: '全部角色', value: 'all' }, ...ROLE_OPTIONS]}
          />
          <Button type="primary" onClick={() => setQueryUser((s) => ({ ...s, page: 1 }))}>查询</Button>
          <Button onClick={() => setQueryUser({ q: '', role: 'all', page: 1, page_size: 10 })}>重置</Button>
          <Button
            type="primary"
            onClick={() => {
              setEditingUserId(null);
              userForm.resetFields();
              userForm.setFieldValue('role', 'city_viewer');
              setUserModalOpen(true);
            }}
          >
            新增系统号
          </Button>
        </Space>
        <Card size="small" title="批量绑定/解绑" style={{ marginTop: 12, marginBottom: 12 }}>
          <Form form={batchForm} layout="inline" onFinish={onBatchBind}>
            <Form.Item label="已选系统号">
              <Tag color="blue">{selectedUserRowKeys.length} 个</Tag>
            </Form.Item>
            <Form.Item name="op" label="操作" initialValue="add" rules={[{ required: true }]}>
              <Select
                style={{ width: 160 }}
                options={[
                  { label: '追加城市', value: 'add' },
                  { label: '移除城市', value: 'remove' },
                  { label: '替换城市', value: 'replace' },
                ]}
              />
            </Form.Item>
            <Form.Item name="tenants" label="城市" rules={[{ required: true }]}>
              <Select mode="multiple" allowClear style={{ minWidth: 320 }} options={cityOptions} />
            </Form.Item>
            <Form.Item>
              <Button type="primary" htmlType="submit" disabled={!selectedUserRowKeys.length}>执行批量</Button>
            </Form.Item>
          </Form>
        </Card>
        <Table
          style={{ marginTop: 12 }}
          rowKey="system_user"
          loading={loadingUsers}
          rowSelection={{
            selectedRowKeys: selectedUserRowKeys,
            onChange: (keys) => setSelectedUserRowKeys(keys),
          }}
          columns={[
            { title: '系统号', dataIndex: 'system_user', width: 180 },
            { title: '角色', dataIndex: 'role', width: 180 },
            {
              title: '可见城市',
              dataIndex: 'tenants',
              render: (v) => (Array.isArray(v) ? v.join(', ') : ''),
            },
            {
              title: '操作',
              width: 180,
              render: (_, row) => (
                <Space>
                  <Button size="small" onClick={() => onEditUser(row)}>编辑</Button>
                  <Popconfirm title="确认删除该系统号？" onConfirm={() => onDeleteUser(row.system_user)}>
                    <Button size="small" danger>删除</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
          dataSource={visibleUsers}
          pagination={{
            current: queryUser.page,
            pageSize: queryUser.page_size,
            total: userTotal,
            onChange: (page, pageSize) => setQueryUser((s) => ({ ...s, page, page_size: pageSize })),
          }}
        />
      </Card>
      <Modal
        title={editingCityId ? '编辑城市' : '新增城市'}
        open={cityModalOpen}
        onCancel={() => {
          setCityModalOpen(false);
          setEditingCityId(null);
          cityForm.resetFields();
        }}
        onOk={() => cityForm.submit()}
        confirmLoading={citySaving}
        okText={editingCityId ? '更新' : '新增'}
        destroyOnClose
      >
        <Form form={cityForm} layout="vertical" onFinish={onCreateCity}>
          <Form.Item name="tenant_id" label="城市ID" rules={[{ required: true, message: '请输入城市ID' }]}>
            <Input placeholder="例如 hz2" disabled={!!editingCityId} />
          </Form.Item>
          <Form.Item name="name" label="城市名称" rules={[{ required: true, message: '请输入城市名称' }]}>
            <Input placeholder="例如 杭州二部" />
          </Form.Item>
          <Form.Item name="is_active" label="状态" initialValue={true}>
            <Select
              options={[
                { label: '启用', value: true },
                { label: '停用', value: false },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={editingUserId ? '编辑系统号权限' : '新增系统号权限'}
        open={userModalOpen}
        onCancel={() => {
          setUserModalOpen(false);
          setEditingUserId(null);
          userForm.resetFields();
        }}
        onOk={() => userForm.submit()}
        confirmLoading={userSaving}
        okText={editingUserId ? '更新' : '新增'}
        destroyOnClose
      >
        <Form form={userForm} layout="vertical" onFinish={onCreateUser}>
          <Form.Item name="system_user" label="系统号" rules={[{ required: true, message: '请输入系统号' }]}>
            <Input placeholder="例如 teacher_bj" disabled={!!editingUserId} />
          </Form.Item>
          <Form.Item name="role" label="角色" initialValue="city_viewer" rules={[{ required: true }]}>
            <Select options={ROLE_OPTIONS} />
          </Form.Item>
          <Form.Item name="tenants" label="可见城市">
            <Select mode="multiple" allowClear options={cityOptions} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
