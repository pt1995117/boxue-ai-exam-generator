import React, { useEffect, useState } from 'react';
import { Alert, Button, Card, Form, Input, Space, Tag, Typography, message } from 'antd';
import { getAdminKeyConfig, updateAdminKeyConfig } from '../services/api';

const DEFAULT_TEMPLATE = `AIT_API_KEY=
AIT_BASE_URL=https://openapi-ait.ke.com
AIT_MODEL=

# 可选：不同节点使用不同模型（不填则默认使用 AIT_MODEL）
ROUTER_MODEL=
SPECIALIST_MODEL=
WRITER_MODEL=
CALC_MODEL=

# 离线 Judge 专用模型（不填则默认 gpt-5.2）
AIT_JUDGE_MODEL=

# 可选：版本发布一键 Git 提交凭证（HTTPS 仓库时必填；SSH 可不填）
GIT_REPO_URL=git@git.lianjia.com:confucius/huaqiao_vibe/boxue-ai-exam-generator.git
GIT_USERNAME=
GIT_TOKEN=
GIT_USER_EMAIL=
GIT_USER_NAME=
`;

export default function GlobalKeyConfigPage() {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [meta, setMeta] = useState(null);

  const loadData = async () => {
    setLoading(true);
    try {
      const res = await getAdminKeyConfig();
      setMeta(res || null);
      form.setFieldsValue({ content: String(res?.content || '') });
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '加载全局 Key 配置失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  const onSave = async (values) => {
    setSaving(true);
    try {
      const payload = { content: String(values?.content || '') };
      const res = await updateAdminKeyConfig(payload);
      setMeta(res?.item || null);
      message.success('全局 Key 配置已保存并自动生效');
      await loadData();
    } catch (e) {
      message.error(e?.response?.data?.error?.message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Alert
        type="info"
        showIcon
        message="平台全局 Key 配置"
        description="这里保存的是全局填写您的Key配置，所有城市统一复用；无需逐城市配置。保存后后端会自动加载。"
      />

      <Card
        title="填写您的Key（全局）"
        extra={(
          <Space>
            <Button onClick={() => form.setFieldsValue({ content: DEFAULT_TEMPLATE })}>
              使用模板
            </Button>
            <Button onClick={loadData} loading={loading}>
              刷新
            </Button>
          </Space>
        )}
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Typography.Text type="secondary">
            文件路径：{meta?.path || '-'}
          </Typography.Text>
          <Space wrap>
            <Tag color={meta?.exists ? 'green' : 'default'}>{meta?.exists ? '文件已存在' : '文件不存在'}</Tag>
            <Tag color={meta?.has_ait_api_key ? 'green' : 'orange'}>AIT_API_KEY {meta?.has_ait_api_key ? '已配置' : '未配置'}</Tag>
            <Tag color={meta?.has_openai_api_key ? 'green' : 'default'}>OPENAI_API_KEY {meta?.has_openai_api_key ? '已配置' : '未配置'}</Tag>
            <Tag color={meta?.has_deepseek_api_key ? 'green' : 'default'}>DEEPSEEK_API_KEY {meta?.has_deepseek_api_key ? '已配置' : '未配置'}</Tag>
            <Tag color={meta?.has_critic_api_key ? 'green' : 'default'}>CRITIC_API_KEY {meta?.has_critic_api_key ? '已配置' : '未配置'}</Tag>
            <Tag color={meta?.has_git_username ? 'green' : 'default'}>GIT_USERNAME {meta?.has_git_username ? '已配置' : '未配置'}</Tag>
            <Tag color={meta?.has_git_token ? 'green' : 'orange'}>GIT_TOKEN {meta?.has_git_token ? '已配置' : '未配置'}</Tag>
          </Space>
          <Form form={form} layout="vertical" onFinish={onSave}>
            <Form.Item
              label="Key 内容（KEY=VALUE，每行一个）"
              name="content"
              rules={[{ required: true, message: '请填写配置内容' }]}
            >
              <Input.TextArea
                autoSize={{ minRows: 14, maxRows: 24 }}
                placeholder="例如：AIT_API_KEY=xxx"
              />
            </Form.Item>
            <Form.Item style={{ marginBottom: 0 }}>
              <Button type="primary" htmlType="submit" loading={saving}>
                保存并自动加载
              </Button>
            </Form.Item>
          </Form>
        </Space>
      </Card>
    </Space>
  );
}
