import React, { useEffect, useState } from 'react';
import {
  Button,
  Card,
  Checkbox,
  Input,
  Select,
  Space,
  Table,
  Typography,
  message,
} from 'antd';
import { createQaRelease, getQaReleases, listQaRuns } from '../services/api';
import { getGlobalTenantId, subscribeGlobalTenant } from '../services/tenantScope';

const { TextArea } = Input;
const { Text } = Typography;

function apiErrMsg(e, fallback) {
  const msg = e?.response?.data?.error?.message
    || e?.response?.data?.message
    || e?.message
    || '';
  const status = e?.response?.status;
  const url = e?.config?.url || '';
  return [fallback, status ? `status=${status}` : '', url ? `url=${url}` : '', msg].filter(Boolean).join(' | ');
}

export default function VersionManagementPage() {
  const [tenantId, setTenantId] = useState(getGlobalTenantId());
  const [loading, setLoading] = useState(false);
  const [releases, setReleases] = useState([]);
  const [runs, setRuns] = useState([]);
  const [version, setVersion] = useState('');
  const [releaseNotes, setReleaseNotes] = useState('');
  const [runIds, setRunIds] = useState([]);
  const [triggerGitCommit, setTriggerGitCommit] = useState(false);
  const [publishing, setPublishing] = useState(false);

  useEffect(() => subscribeGlobalTenant((tid) => setTenantId(tid)), []);

  const loadAll = async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const [releasesRes, runsRes] = await Promise.all([
        getQaReleases(tenantId),
        listQaRuns(tenantId, { days: 90, success_only: 1, page: 1, page_size: 200 }),
      ]);
      setReleases(releasesRes?.items || []);
      setRuns(runsRes?.items || []);
    } catch (e) {
      message.error(apiErrMsg(e, '加载版本与 run 列表失败'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAll();
  }, [tenantId]); // eslint-disable-line react-hooks/exhaustive-deps

  const eligibleRuns = runs.filter((r) => r.release_eligible === true);
  const lastRelease = releases[0];

  const onPublish = async () => {
    if (!tenantId) return;
    const v = String(version || '').trim();
    const notes = String(releaseNotes || '').trim();
    const selectedRunIds = (Array.isArray(runIds) ? runIds : []).map((x) => String(x || '').trim()).filter(Boolean);
    if (!v) {
      message.warning('请填写版本号');
      return;
    }
    if (!selectedRunIds.length) {
      message.warning('请选择至少 1 个 run 作为发布质量评估基准');
      return;
    }
    setPublishing(true);
    try {
      const res = await createQaRelease(tenantId, {
        version: v,
        release_notes: notes,
        run_ids: selectedRunIds,
        trigger_git_commit: triggerGitCommit,
      });
      message.success(`版本 ${v} 已发布`);
      if (res?.git?.ok === false && res?.git?.error) {
        message.warning(`Git 提交未执行: ${res.git.message || res.git.error}`);
      }
      setVersion('');
      setReleaseNotes('');
      setRunIds([]);
      await loadAll();
    } catch (e) {
      message.error(apiErrMsg(e, '发布失败'));
    } finally {
      setPublishing(false);
    }
  };

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Card title="发布版本">
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Text type="secondary">
            发布前须完成：1）跑一次出题任务并有题目落库；2）对该 run 的落库题目跑过离线 Judge。仅满足条件的 run 可选。
          </Text>
          <Space wrap align="center">
            <Input
              placeholder="版本号（如 v1.0.0）"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
              style={{ width: 180 }}
            />
            <Select
              mode="multiple"
              placeholder="选择 run（可多选，仅显示可发布的 run）"
              value={runIds}
              onChange={setRunIds}
              style={{ width: 560 }}
              showSearch
              optionFilterProp="label"
              options={eligibleRuns.map((r) => ({
                label: `${r.run_id} | saved=${r.saved_count} | ${r.ended_at || ''}`,
                value: r.run_id,
              }))}
            />
          </Space>
          <TextArea
            placeholder="版本更新说明"
            value={releaseNotes}
            onChange={(e) => setReleaseNotes(e.target.value)}
            rows={3}
            style={{ maxWidth: 560 }}
          />
          <Space>
            <Checkbox checked={triggerGitCommit} onChange={(e) => setTriggerGitCommit(e.target.checked)}>
              同时提交 Git（将发布记录提交到当前仓库）
            </Checkbox>
            <Button type="primary" loading={publishing} onClick={onPublish} disabled={!version || runIds.length === 0}>
              发布版本
            </Button>
          </Space>
        </Space>
      </Card>

      <Card title="版本记录">
        <Text type="secondary">质量评估中的「漂移对比」「发布评估」将优先以最新已发布版本作为基线 run。</Text>
        <Table
          size="small"
          loading={loading}
          rowKey={(r) => `${r.version}-${r.published_at}`}
          dataSource={releases}
          pagination={{ pageSize: 20 }}
          style={{ marginTop: 12 }}
          columns={[
            { title: '版本号', dataIndex: 'version', width: 140 },
            { title: '更新说明', dataIndex: 'release_notes', ellipsis: true, render: (t) => (t || '—').slice(0, 80) + ((t || '').length > 80 ? '…' : '') },
            {
              title: '基准 run',
              dataIndex: 'run_ids',
              width: 420,
              ellipsis: true,
              render: (_, r) => {
                const ids = Array.isArray(r?.run_ids) && r.run_ids.length
                  ? r.run_ids
                  : (r?.run_id ? [r.run_id] : []);
                return ids.length ? ids.join(', ') : '—';
              },
            },
            { title: '发布时间', dataIndex: 'published_at', width: 220 },
            { title: '发布人', dataIndex: 'published_by', width: 120 },
          ]}
        />
        {lastRelease && (
          <Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
            当前基线版本：{lastRelease.version}（run: {Array.isArray(lastRelease.run_ids) && lastRelease.run_ids.length ? lastRelease.run_ids.join(', ') : lastRelease.run_id}）
          </Text>
        )}
      </Card>
    </Space>
  );
}
