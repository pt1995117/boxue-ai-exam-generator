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

export default function QuestionDetailView({ question }) {
  const q = question || {};
  const optionRows = getOptionRows(q);
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={10}>
      <Descriptions size="small" bordered column={3}>
        <Descriptions.Item label="答案">{q['正确答案'] || '（空）'}</Descriptions.Item>
        <Descriptions.Item label="难度值">{q['难度值'] ?? '（空）'}</Descriptions.Item>
        <Descriptions.Item label="来源切片">{q['来源路径'] || '（空）'}</Descriptions.Item>
      </Descriptions>
      <div>
        <Typography.Text strong>题干</Typography.Text>
        <Typography.Paragraph style={{ margin: '6px 0 0 0', whiteSpace: 'pre-wrap' }}>
          {q['题干'] || q.question || '（空）'}
        </Typography.Paragraph>
      </div>
      <div>
        <Typography.Text strong>选项</Typography.Text>
        <Space direction="vertical" size={4} style={{ width: '100%', marginTop: 6 }}>
          {optionRows.length === 0 ? (
            <Typography.Text>（空）</Typography.Text>
          ) : optionRows.map((opt) => (
            <Typography.Text key={opt.key}>{`${opt.key}. ${opt.value}`}</Typography.Text>
          ))}
        </Space>
      </div>
      <div>
        <Typography.Text strong>解析</Typography.Text>
        <Typography.Paragraph style={{ margin: '6px 0 0 0', whiteSpace: 'pre-wrap' }}>
          {q['解析'] || q.explanation || '（空）'}
        </Typography.Paragraph>
      </div>
    </Space>
  );
}
