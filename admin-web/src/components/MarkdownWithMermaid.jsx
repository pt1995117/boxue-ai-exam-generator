import React, { useEffect, useId, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import mermaid from 'mermaid';

let mermaidReady = false;

const ensureMermaid = () => {
  if (mermaidReady) return;
  mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' });
  mermaidReady = true;
};

const OP_LABELS = new Set(['=', '×', 'x', 'X', '*', '÷', '/', '+', '-']);

const parseNodeToken = (token) => {
  const t = String(token || '').trim();
  const m = t.match(/^([A-Za-z0-9_\u4e00-\u9fa5]+)\[(.+?)\]$/);
  if (m) return { id: m[1], label: m[2].trim() };
  return { id: t, label: null };
};

const sanitizeMermaidCode = (code) => {
  const rawLines = String(code || '').split('\n');
  const lines = [];
  const labels = {};
  let edges = [];
  const others = [];
  let autoId = 1;

  const newTmpId = (prefix) => {
    const id = `${prefix}_${autoId}`;
    autoId += 1;
    return id;
  };

  const parseFormulaLine = (line) => {
    const s = String(line || '').trim();
    if (!s || s.includes('-->') || s.includes('<--') || s.includes('---')) return null;
    const m = s.match(/^([A-Za-z0-9_\u4e00-\u9fa5]+\[(.+?)\])\s*=\s*(.+)$/);
    if (!m) return null;
    const lhs = parseNodeToken(m[1]);
    if (!lhs?.id) return null;
    const rhs = m[3].trim();
    const segs = rhs.split(/\s*([×xX*÷/+\-])\s*/).filter((v) => String(v || '').trim() !== '');
    const termTokens = segs.filter((_, i) => i % 2 === 0);
    const ops = segs.filter((_, i) => i % 2 === 1);
    if (!termTokens.length) return null;
    const termNodes = termTokens.map((t, i) => {
      const parsed = parseNodeToken(t);
      if (parsed.id && parsed.label !== null) return parsed;
      const tmp = newTmpId(`FTERM${i + 1}`);
      return { id: tmp, label: t.trim() };
    });
    const eqId = newTmpId('OP_EQ');
    const normalized = [];
    normalized.push(`  ${lhs.id}[${lhs.label || lhs.id}] --> ${eqId}[=]`);
    if (termNodes[0]) {
      normalized.push(`  ${eqId}[=] --> ${termNodes[0].id}[${termNodes[0].label || termNodes[0].id}]`);
    }
    let prevOp = eqId;
    for (let i = 0; i < ops.length; i += 1) {
      const opId = newTmpId(`OP_${i + 1}`);
      normalized.push(`  ${prevOp}[${i === 0 ? '=' : ops[i - 1]}] --> ${opId}[${ops[i]}]`);
      const term = termNodes[i + 1];
      if (term) {
        normalized.push(`  ${opId}[${ops[i]}] --> ${term.id}[${term.label || term.id}]`);
      }
      prevOp = opId;
    }
    return normalized;
  };

  for (const line of rawLines) {
    const converted = parseFormulaLine(line);
    if (converted?.length) {
      lines.push(...converted);
    } else {
      lines.push(line);
    }
  }

  for (const line of lines) {
    const s = line.trim();
    if (!s.includes('-->')) {
      const m = s.match(/^([A-Za-z0-9_\u4e00-\u9fa5]+)\[(.+?)\]$/);
      if (m) labels[m[1]] = m[2].trim();
      others.push(line);
      continue;
    }
    const parts = s.split('-->');
    if (parts.length !== 2) {
      others.push(line);
      continue;
    }
    const left = parseNodeToken(parts[0]);
    const right = parseNodeToken(parts[1]);
    if (left.label) labels[left.id] = left.label;
    if (right.label) labels[right.id] = right.label;
    edges.push([left.id, right.id]);
  }

  const isOp = (id) => OP_LABELS.has(String(labels[id] || '').trim());
  const normalizeOp = (op) => {
    if (op === 'x' || op === 'X' || op === '*') return '×';
    if (op === '/') return '÷';
    return op;
  };
  const nodeText = (id) => String(labels[id] || id || '').trim();
  const outgoing = (id) => edges.filter(([a]) => a === id).map(([, b]) => b);
  const incoming = (id) => edges.filter(([, b]) => b === id).map(([a]) => a);

  const buildEquationRhs = (eqId) => {
    const terms = [];
    const ops = [];
    let cur = eqId;
    const seen = new Set();
    while (cur && !seen.has(cur)) {
      seen.add(cur);
      const outs = outgoing(cur);
      if (!outs.length) break;
      const nonOps = outs.filter((n) => !isOp(n));
      for (const n of nonOps) {
        const txt = nodeText(n);
        if (txt) terms.push(txt);
      }
      const opChild = outs.find((n) => isOp(n));
      if (!opChild) break;
      const opTxt = normalizeOp(nodeText(opChild));
      if (opTxt && opTxt !== '=') ops.push(opTxt);
      cur = opChild;
    }
    if (!terms.length) return '';
    if (!ops.length || terms.length === 1) return terms.join(' ');
    const out = [terms[0]];
    for (let i = 1; i < terms.length; i += 1) {
      const op = ops[i - 1] || ops[ops.length - 1];
      out.push(op, terms[i]);
    }
    return out.join(' ');
  };

  const eqIds = Object.keys(labels).filter((id) => String(labels[id] || '').trim() === '=');
  for (const eq of eqIds) {
    const rhs = buildEquationRhs(eq);
    if (!rhs) continue;
    const lhsNodes = incoming(eq).filter((n) => !isOp(n));
    for (const lhs of lhsNodes) {
      const lhsTxt = nodeText(lhs);
      if (!lhsTxt) continue;
      labels[lhs] = `${lhsTxt} = ${rhs}`;
    }
  }

  let changed = true;
  while (changed) {
    changed = false;
    const opIds = Object.keys(labels).filter(isOp);
    for (const op of opIds) {
      const incoming = edges.filter(([, b]) => b === op);
      const outgoing = edges.filter(([a]) => a === op);
      if (!incoming.length && !outgoing.length) continue;
      const newEdges = [];
      if (incoming.length && outgoing.length) {
        for (const [p] of incoming) {
          for (const [, c] of outgoing) {
            if (p !== c) newEdges.push([p, c]);
          }
        }
      }
      edges = edges.filter(([a, b]) => a !== op && b !== op);
      for (const e of newEdges) {
        if (!edges.some(([a, b]) => a === e[0] && b === e[1])) edges.push(e);
      }
      changed = true;
    }
  }
  edges = edges.filter(([a, b]) => !isOp(a) && !isOp(b));

  const rebuilt = [];
  for (const line of others) {
    const s = line.trim();
    const m = s.match(/^([A-Za-z0-9_\u4e00-\u9fa5]+)\[(.+?)\]$/);
    if (m && OP_LABELS.has(m[2].trim())) continue;
    rebuilt.push(line);
  }
  for (const [a, b] of edges) {
    const left = labels[a] ? `${a}[${labels[a]}]` : a;
    const right = labels[b] ? `${b}[${labels[b]}]` : b;
    rebuilt.push(`  ${left} --> ${right}`);
  }
  return rebuilt.join('\n');
};

const MermaidBlock = ({ code }) => {
  const id = useId();
  const ref = useRef(null);
  const [error, setError] = useState('');

  useEffect(() => {
    ensureMermaid();
    let cancelled = false;
    if (!ref.current) return undefined;
    const render = async () => {
      try {
        const sanitized = sanitizeMermaidCode(code);
        await mermaid.parse(sanitized);
        const { svg } = await mermaid.render(`mermaid_${id.replace(/[:]/g, '_')}`, sanitized);
        // mermaid 某些版本在语法错误时会返回“炸弹图”SVG而不是抛异常，这里主动拦截并回退文本。
        if (/Syntax error in text|mermaid version/i.test(String(svg || ''))) {
          throw new Error('Mermaid syntax error');
        }
        if (cancelled) return;
        ref.current.innerHTML = svg;
        setError('');
      } catch (e) {
        if (cancelled) return;
        setError(e?.message || 'Mermaid render failed');
      }
    };
    render();
    return () => {
      cancelled = true;
    };
  }, [code, id]);

  if (error) {
    return (
      <div>
        <div style={{ color: '#999', marginBottom: 6 }}>Mermaid 渲染失败，已回退为文本：{error}</div>
        <pre style={{ margin: 0, overflowX: 'auto' }}>
          <code>{String(code || '').trim()}</code>
        </pre>
      </div>
    );
  }
  return <div ref={ref} style={{ maxWidth: '100%', overflowX: 'auto' }} />;
};

const MERMAID_DIAGRAM_PREFIXES = [
  'graph',
  'flowchart',
  'sequencediagram',
  'classdiagram',
  'statediagram',
  'erdiagram',
  'journey',
  'gantt',
  'pie',
  'mindmap',
  'timeline',
  'gitgraph',
  'quadrantchart',
  'sankey',
  'requirementdiagram',
  'block',
  'xychart',
  'c4context',
  'c4container',
  'c4component',
  'c4dynamic',
  'c4deployment',
];

const shouldRenderMermaid = (code) => {
  const raw = String(code || '').trim();
  if (!raw) return false;
  const first = raw
    .split('\n')
    .map((line) => String(line || '').trim())
    .find((line) => line && !line.startsWith('%%'));
  if (!first) return false;
  const lower = first.toLowerCase();
  return MERMAID_DIAGRAM_PREFIXES.some((prefix) => lower.startsWith(prefix));
};

export default function MarkdownWithMermaid({
  text,
  enableMermaid = true,
  disableStrikethrough = false,
}) {
  const content = String(text || '');
  if (!content) return <span>（空）</span>;
  if (enableMermaid) ensureMermaid();
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code({ inline, className, children, ...props }) {
          const lang = String(className || '').replace('language-', '');
          const rawCode = String(children || '').trim();
          if (!inline && lang === 'mermaid') {
            if (enableMermaid && shouldRenderMermaid(rawCode)) {
              return <MermaidBlock code={rawCode} />;
            }
            return (
              <pre className={className} {...props} style={{ overflowX: 'auto' }}>
                <code>{children}</code>
              </pre>
            );
          }
          if (inline) {
            return (
              <code className={className} {...props}>
                {children}
              </code>
            );
          }
          return (
            <pre className={className} {...props} style={{ overflowX: 'auto' }}>
              <code>{children}</code>
            </pre>
          );
        },
        table({ children }) {
          return (
            <div style={{ overflowX: 'auto' }}>
              <table
                style={{
                  borderCollapse: 'collapse',
                  width: '100%',
                  minWidth: 560,
                  fontSize: 14,
                }}
              >
                {children}
              </table>
            </div>
          );
        },
        th({ children }) {
          return (
            <th
              style={{
                border: '1px solid #d9d9d9',
                padding: '8px 10px',
                background: '#fafafa',
                fontWeight: 600,
                textAlign: 'left',
                verticalAlign: 'top',
                whiteSpace: 'pre-wrap',
              }}
            >
              {children}
            </th>
          );
        },
        td({ children }) {
          return (
            <td
              style={{
                border: '1px solid #e5e5e5',
                padding: '8px 10px',
                textAlign: 'left',
                verticalAlign: 'top',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {children}
            </td>
          );
        },
        del({ children }) {
          if (disableStrikethrough) return <span>{children}</span>;
          return <del>{children}</del>;
        },
        a({ href, children }) {
          return (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          );
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}
