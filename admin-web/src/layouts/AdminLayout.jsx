import React, { useEffect, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { Breadcrumb, Button, Input, Layout, Menu, Select, Space, Typography, message } from 'antd';
import { FileSearchOutlined, LinkOutlined, DashboardOutlined, UploadOutlined, RobotOutlined, DatabaseOutlined, TeamOutlined, MenuFoldOutlined, MenuUnfoldOutlined, BookOutlined, LineChartOutlined, TagOutlined, OrderedListOutlined, KeyOutlined, ProfileOutlined } from '@ant-design/icons';
import { getAuthToken, getSystemUser, listTenants, setAuthToken, setSystemUser } from '../services/api';
import { getGlobalTenantId, setGlobalTenantId } from '../services/tenantScope';

const { Header, Sider, Content } = Layout;

const items = [
  { key: '/', icon: <DashboardOutlined />, label: <Link to="/">工作台</Link> },
  { key: '/materials', icon: <UploadOutlined />, label: <Link to="/materials">资源上传</Link> },
  { key: '/slice-review', icon: <FileSearchOutlined />, label: <Link to="/slice-review">切片核对</Link> },
  { key: '/mapping-review', icon: <LinkOutlined />, label: <Link to="/mapping-review">映射确认</Link> },
  { key: '/ai-generate', icon: <RobotOutlined />, label: <Link to="/ai-generate">AI出题</Link> },
  { key: '/generate-templates', icon: <ProfileOutlined />, label: <Link to="/generate-templates">出题模板</Link> },
  { key: '/qa-evaluation', icon: <LineChartOutlined />, label: <Link to="/qa-evaluation">质量评估</Link> },
  { key: '/judge-tasks', icon: <OrderedListOutlined />, label: <Link to="/judge-tasks">Judge任务</Link> },
  { key: '/version-management', icon: <TagOutlined />, label: <Link to="/version-management">版本管理</Link> },
  { key: '/question-bank', icon: <DatabaseOutlined />, label: <Link to="/question-bank">题库</Link> },
  { key: '/city-admin', icon: <TeamOutlined />, label: <Link to="/city-admin">城市管理</Link> },
  { key: '/global-key-config', icon: <KeyOutlined />, label: <Link to="/global-key-config">全局Key配置</Link> },
];

export default function AdminLayout() {
  const location = useLocation();
  const pathname = location.pathname || '/';
  const selectedMenuKey = pathname.startsWith('/ai-generate/tasks/')
    ? '/ai-generate'
    : (pathname.startsWith('/judge-tasks/') ? '/judge-tasks' : pathname);
  const [collapsed, setCollapsed] = useState(localStorage.getItem('layout_sider_collapsed') === '1');
  const [tenants, setTenants] = useState([]);
  const [globalTenantId, setGlobalTenantIdState] = useState(getGlobalTenantId());
  const [globalSystemUser, setGlobalSystemUser] = useState(getSystemUser());
  const [globalAuthToken, setGlobalAuthToken] = useState(getAuthToken());
  const routeNameMap = {
    '/': '工作台',
    '/materials': '资源上传',
    '/slice-review': '切片核对',
    '/mapping-review': '映射确认',
    '/ai-generate': 'AI出题',
    '/generate-templates': '出题模板',
    '/qa-evaluation': '质量评估',
    '/judge-tasks': 'Judge任务',
    '/version-management': '版本管理',
    '/question-bank': '题库',
    '/city-admin': '城市管理',
    '/global-key-config': '全局Key配置',
  };
  const currentName = routeNameMap[location.pathname] || '页面';
  const breadcrumbItems = [
    { title: <Link to="/">首页</Link> },
    ...(location.pathname === '/' ? [] : [{ title: currentName }]),
  ];

  useEffect(() => {
    listTenants()
      .then((data) => {
        const items = data.items || [];
        setTenants(items);
        if (!items.length) return;
        const exists = items.some((x) => x.tenant_id === globalTenantId);
        const chosen = exists ? globalTenantId : items[0].tenant_id;
        if (chosen !== globalTenantId) {
          setGlobalTenantIdState(chosen);
          setGlobalTenantId(chosen);
        }
      })
      .catch((e) => message.error(e?.response?.data?.error?.message || '加载城市失败'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Layout className="admin-shell">
      <Sider theme="light" width={220} collapsible collapsed={collapsed} trigger={null} collapsedWidth={64}>
        <div style={{ padding: 16, borderBottom: '1px solid #f0f0f0' }}>
          <Space size={10} style={{ width: '100%', justifyContent: collapsed ? 'center' : 'flex-start' }}>
            <BookOutlined style={{ fontSize: 18, color: '#1677ff' }} />
            {!collapsed && (
              <Typography.Title level={5} style={{ margin: 0 }}>
                认证练习管理后台
              </Typography.Title>
            )}
          </Space>
        </div>
        <Menu mode="inline" selectedKeys={[selectedMenuKey]} items={items} style={{ height: '100%' }} />
      </Sider>
      <Layout>
        <Header style={{ background: '#fff', borderBottom: '1px solid #f0f0f0', padding: '10px 20px', height: 'auto', lineHeight: 'normal' }}>
          <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start">
            <div>
              <Space size={8} style={{ marginBottom: 6 }}>
                <Button
                  size="small"
                  icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                  onClick={() => {
                    const next = !collapsed;
                    setCollapsed(next);
                    localStorage.setItem('layout_sider_collapsed', next ? '1' : '0');
                  }}
                />
                <Breadcrumb items={breadcrumbItems} separator=">" />
              </Space>
            </div>
            <Space>
              <Typography.Text type="secondary">城市</Typography.Text>
              <Select
                value={globalTenantId || undefined}
                style={{ width: 180 }}
                placeholder="选择城市"
                onChange={(v) => {
                  setGlobalTenantIdState(v);
                  setGlobalTenantId(v);
                }}
                options={tenants.map((t) => ({ label: `${t.name} (${t.tenant_id})`, value: t.tenant_id }))}
              />
              <Typography.Text type="secondary">用户</Typography.Text>
              <Input
                value={globalSystemUser}
                style={{ width: 140 }}
                onChange={(e) => setGlobalSystemUser(e.target.value)}
                onBlur={() => setSystemUser(globalSystemUser)}
              />
              <Input.Password
                value={globalAuthToken}
                placeholder="OIDC Token(可选)"
                style={{ width: 220 }}
                onChange={(e) => setGlobalAuthToken(e.target.value)}
                onBlur={() => setAuthToken(globalAuthToken)}
              />
            </Space>
          </Space>
        </Header>
        <Content style={{ padding: 20 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
