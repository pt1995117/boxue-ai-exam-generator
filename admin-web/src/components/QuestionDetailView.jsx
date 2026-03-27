import React from 'react';
import { Descriptions, Space, Typography } from 'antd';

function getOptionRows(question) {
  const rows = [];
  for (let i = 1; i <= 8; i += 1) {
    const value = String(question?.[`选项${i}`] || '').trim();
    if (!value) continue;
    rows.push({ key: String.fromCharCode(64 + i), value });
  }
  if (!rows.length && Array.isArray(question?.options)) {
    question.options.forEach((v, idx) => {
      const value = String(v || '').trim();
      if (!value) return;
      rows.push({ key: String.fromCharCode(65 + idx), value });
    });
  }
  return rows;
}

function getMotherQuestionText(question) {
  const q = question || {};
  const fullTextFields = ['参考母题全文', 'mother_questions_full_text'];
  for (const key of fullTextFields) {
    const value = String(q?.[key] || '').trim();
    if (value) return value;
  }
  if (Array.isArray(q?.mother_questions_full)) {
    const rows = q.mother_questions_full.filter((x) => x && typeof x === 'object');
    if (rows.length) {
      return rows.map((row, idx) => {
        const stem = String(row['题干'] || row.question || '').trim() || '（无）';
        const answer = String(row['正确答案'] || row.answer || '').trim() || '（无）';
        const explanation = String(row['解析'] || row.explanation || '').trim() || '（无）';
        const optionDict = row['选项'] && typeof row['选项'] === 'object' ? row['选项'] : {};
        const optionRows = [];
        for (let i = 1; i <= 8; i += 1) {
          const key = String.fromCharCode(64 + i);
          const text = String(optionDict[key] || row[`选项${i}`] || '').trim();
          if (!text) continue;
          optionRows.push(`${key}. ${text}`);
        }
        const optionText = optionRows.length ? optionRows.join('\n') : '（无）';
        return `母题${idx + 1}\n题干：${stem}\n选项：\n${optionText}\n正确答案：${answer}\n解析：${explanation}`;
      }).join('\n\n');
    }
  }
  const directFields = ['关联母题', '母题题干', '母题', 'parent_question', 'mother_question'];
  for (const key of directFields) {
    const value = String(q?.[key] || '').trim();
    if (value) return value;
  }
  if (Array.isArray(q?.mother_questions)) {
    const rows = q.mother_questions.map((x) => String(x || '').trim()).filter(Boolean);
    if (rows.length) return rows.map((x, i) => `${i + 1}. ${x}`).join('\n');
  }
  if (Array.isArray(q?.examples)) {
    const rows = q.examples
      .map((ex) => {
        if (ex && typeof ex === 'object') {
          return String(ex['题干'] || ex.question || '').trim();
        }
        return String(ex || '').trim();
      })
      .filter(Boolean);
    if (rows.length) return rows.map((x, i) => `${i + 1}. ${x}`).join('\n');
  }
  return '';
}

function getRelatedSlicePaths(question) {
  const q = question || {};
  const candidates = [
    q['关联切片路径'],
    q.related_slice_paths,
    q.critic_basis_paths,
    q['关联切片路径文本'],
  ];
  const rows = [];
  const seen = new Set();
  const pushRow = (value) => {
    const text = String(value || '').trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    rows.push(text);
  };
  candidates.forEach((candidate) => {
    if (Array.isArray(candidate)) {
      candidate.forEach((x) => pushRow(x));
      return;
    }
    if (typeof candidate === 'string' && candidate.trim()) {
      try {
        const parsed = JSON.parse(candidate);
        if (Array.isArray(parsed)) {
          parsed.forEach((x) => pushRow(x));
          return;
        }
      } catch (_) {}
      candidate
        .split(/\n|;|,/g)
        .map((x) => x.trim())
        .filter(Boolean)
        .forEach((x) => pushRow(x));
    }
  });
  return rows;
}

function getSourceSliceContent(question) {
  const q = question || {};
  const candidates = [
    q['切片原文'],
    q.slice_content,
    q.textbook_slice,
    q['来源切片原文'],
  ];
  for (const v of candidates) {
    const text = String(v || '').trim();
    if (text) return text;
  }
  return '';
}

function getAllSliceContent(question) {
  const q = question || {};
  const candidates = [
    q['全部切片原文'],
    q.all_slice_text,
    q['关联切片原文'],
  ];
  for (const v of candidates) {
    const text = String(v || '').trim();
    if (text) return text;
  }
  return '';
}

export default function QuestionDetailView({ question }) {
  const q = question || {};
  const optionRows = getOptionRows(q);
  const motherQuestionText = getMotherQuestionText(q);
  const relatedSlicePaths = getRelatedSlicePaths(q);
  const sourceSliceContent = getSourceSliceContent(q);
  const allSliceContent = getAllSliceContent(q);
  const textWrapStyle = { whiteSpace: 'pre-wrap', wordBreak: 'break-all' };
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={10}>
      <Descriptions size="small" bordered column={3}>
        <Descriptions.Item label="答案">{q['正确答案'] || '（空）'}</Descriptions.Item>
        <Descriptions.Item label="难度值">{q['难度值'] ?? '（空）'}</Descriptions.Item>
        <Descriptions.Item label="来源切片">
          <Typography.Text style={textWrapStyle}>{q['来源路径'] || '（空）'}</Typography.Text>
        </Descriptions.Item>
        <Descriptions.Item label="关联切片路径" span={3}>
          <Typography.Text style={textWrapStyle}>
            {relatedSlicePaths.length ? relatedSlicePaths.map((x, i) => `${i + 1}. ${x}`).join('\n') : '（空）'}
          </Typography.Text>
        </Descriptions.Item>
        <Descriptions.Item label="来源切片原文" span={3}>
          <Typography.Text style={textWrapStyle}>
            {sourceSliceContent || '（空）'}
          </Typography.Text>
        </Descriptions.Item>
        <Descriptions.Item label="全部切片原文" span={3}>
          <Typography.Text style={textWrapStyle}>
            {allSliceContent || '（空）'}
          </Typography.Text>
        </Descriptions.Item>
        <Descriptions.Item label="关联母题" span={3}>
          <Typography.Text style={textWrapStyle}>
            {motherQuestionText || '（空）'}
          </Typography.Text>
        </Descriptions.Item>
      </Descriptions>
      <div>
        <Typography.Text strong>题干</Typography.Text>
        <Typography.Paragraph style={{ margin: '6px 0 0 0', ...textWrapStyle }}>
          {q['题干'] || q.question || '（空）'}
        </Typography.Paragraph>
      </div>
      <div>
        <Typography.Text strong>选项</Typography.Text>
        <Space direction="vertical" size={4} style={{ width: '100%', marginTop: 6 }}>
          {optionRows.length === 0 ? (
            <Typography.Text>（空）</Typography.Text>
          ) : optionRows.map((opt) => (
            <Typography.Text key={opt.key} style={textWrapStyle}>{`${opt.key}. ${opt.value}`}</Typography.Text>
          ))}
        </Space>
      </div>
      <div>
        <Typography.Text strong>解析</Typography.Text>
        <Typography.Paragraph style={{ margin: '6px 0 0 0', ...textWrapStyle }}>
          {q['解析'] || q.explanation || '（空）'}
        </Typography.Paragraph>
      </div>
    </Space>
  );
}
