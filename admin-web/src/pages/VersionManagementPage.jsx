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
  const [gitRepoUrl, setGitRepoUrl] = useState('https://git.lianjia.com/confucius/huaqiao_vibe/boxue-ai-exam-generator.git');
  const [gitUserEmail, setGitUserEmail] = useState('panting047@ke.com');
  const [gitUserName, setGitUserName] = useState('panting047');
  const [gitCommitMessage, setGitCommitMessage] = useState('[紧急]fix');
  const [gitPushBranch, setGitPushBranch] = useState('main');
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
  const formatRunDuration = (sec) => {
    const n = Number(sec || 0);
    if (!Number.isFinite(n) || n <= 0) return '-';
    if (n < 60) return `${Math.round(n)}s`;
    const m = Math.floor(n / 60);
    const s = Math.round(n % 60);
    if (m < 60) return `${m}m${s}s`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return `${h}h${rm}m`;
  };
  const renderRunTaskName = (r) => {
    const name = String(r?.task_name || '').trim();
    if (name) return name;
    const tid = String(r?.task_id || '').trim();
    if (tid) return `未命名任务(${tid})`;
    return `未命名任务(${String(r?.run_id || '').trim()})`;
  };

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
        git_repo_url: gitRepoUrl,
        git_user_email: gitUserEmail,
        git_user_name: gitUserName,
        git_commit_message: gitCommitMessage,
        git_push_branch: gitPushBranch,
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
              placeholder="按任务名选择 run（可多选，仅显示可发布）"
              value={runIds}
              onChange={setRunIds}
              style={{ width: 560 }}
              showSearch
              optionFilterProp="label"
              options={eligibleRuns.map((r) => ({
                label: `${renderRunTaskName(r)} | Judge:${String(r?.latest_judge_task_name || r?.latest_judge_status || '-')} | 出题耗时:${formatRunDuration(r?.run_duration_sec)} | Judge耗时:${formatRunDuration(r?.latest_judge_duration_sec)} | ${r.run_id} | saved=${r.saved_count} | ${r.ended_at || ''}`,
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
              同时提交 Git（真实 push 到目标仓库）
            </Checkbox>
            <Button type="primary" loading={publishing} onClick={onPublish} disabled={!version || runIds.length === 0}>
              发布版本
            </Button>
          </Space>
          {triggerGitCommit && (
            <Card size="small" title="Git 提交配置" style={{ maxWidth: 900 }}>
              <Space direction="vertical" style={{ width: '100%' }} size={8}>
                <Input
                  value={gitRepoUrl}
                  onChange={(e) => setGitRepoUrl(e.target.value)}
                  placeholder="目标仓库 URL"
                />
                <Space wrap style={{ width: '100%' }}>
                  <Input
                    style={{ width: 260 }}
                    value={gitUserEmail}
                    onChange={(e) => setGitUserEmail(e.target.value)}
                    placeholder="git user.email"
                  />
                  <Input
                    style={{ width: 200 }}
                    value={gitUserName}
                    onChange={(e) => setGitUserName(e.target.value)}
                    placeholder="git user.name"
                  />
                  <Input
                    style={{ width: 160 }}
                    value={gitPushBranch}
                    onChange={(e) => setGitPushBranch(e.target.value)}
                    placeholder="推送分支"
                  />
                </Space>
                <Input
                  value={gitCommitMessage}
                  onChange={(e) => setGitCommitMessage(e.target.value)}
                  placeholder='commit message（如 "[紧急]fix"）'
                />
              </Space>
            </Card>
          )}
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
            {
              title: 'Git结果',
              dataIndex: 'git',
              width: 260,
              render: (_, r) => {
                const g = r?.git || {};
                const ok = g?.ok;
                if (ok === true) return <Text type="success">成功：{g?.commit_message || '-'}</Text>;
                if (ok === false) return <Text type="danger">失败：{g?.error || g?.message || '-'}</Text>;
                return '未执行';
              },
            },
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
