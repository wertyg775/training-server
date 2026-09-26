import React, { useEffect, useRef, useState } from 'react';
import { listExecutions } from './api.js';
import '../styles.css';

function timeAgo(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return 'Just now';
  const [amount, unit] = seconds < 3600
    ? [Math.floor(seconds / 60), 'minute']
    : seconds < 86400
      ? [Math.floor(seconds / 3600), 'hour']
      : [Math.floor(seconds / 86400), 'day'];
  return `${amount} ${unit}${amount === 1 ? '' : 's'} ago`;
}

const STATE_LABELS = {
  building: 'Building image',
  starting: 'Starting',
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

export default function ExecutionsPage({ onNavigate }) {
  const [executions, setExecutions] = useState([]);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const selectAll = useRef(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    listExecutions(controller.signal)
      .then((data) => { if (!controller.signal.aborted) setExecutions(data); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [attempt]);

  const term = query.trim().toLowerCase();
  const visible = executions.filter((execution) => !term
    || execution.id.toLowerCase().includes(term)
    || execution.project_name.toLowerCase().includes(term)
    || execution.entrypoint.toLowerCase().includes(term)
    || (STATE_LABELS[execution.state] || execution.state).toLowerCase().includes(term));
  const showEmptyState = !loading && !error && visible.length === 0 && !term;
  const allSelected = visible.length > 0 && visible.every((execution) => selected.has(execution.id));
  const someSelected = visible.some((execution) => selected.has(execution.id));

  useEffect(() => {
    if (selectAll.current) selectAll.current.indeterminate = someSelected && !allSelected;
  }, [someSelected, allSelected]);

  function toggleSelection(ids, checked) {
    setSelected((previous) => {
      const next = new Set(previous);
      ids.forEach((id) => checked ? next.add(id) : next.delete(id));
      return next;
    });
  }

  return (
    <>
      <header className="brand">Training Server</header>
      <div className="layout">
        <aside className="sidebar">
          <nav className="nav" aria-label="Main">
            <a className="nav-item" href="#" onClick={(event) => { event.preventDefault(); onNavigate('projects'); }}><span className="nav-icon" aria-hidden="true">▣</span>Projects</a>
            <a className="nav-item" href="#" onClick={(event) => { event.preventDefault(); onNavigate('training-jobs'); }}><span className="nav-icon" aria-hidden="true">◉</span>Training Jobs</a>
            <a className="nav-item active" href="#" aria-current="page"><span className="nav-icon" aria-hidden="true">▶</span>Executions</a>
            <a className="nav-item" href="#" onClick={(event) => { event.preventDefault(); onNavigate('outputs'); }}><span className="nav-icon" aria-hidden="true">▤</span>Outputs</a>
            <a className="nav-item" href="#"><span className="nav-icon" aria-hidden="true">⚙</span>Settings</a>
          </nav>
        </aside>
        <div className="content">
          <main className="dashboard">
            <div className="page-head"><h1>Executions</h1></div>
            <section aria-label="Executions" aria-busy={loading}>
              {!showEmptyState && <input className="project-search" type="search" aria-label="Search executions" placeholder="Search executions" value={query} onChange={(event) => setQuery(event.target.value)} />}
              {showEmptyState ? (
                <div className="projects-empty-state" role="status">
                  <div className="empty-state-content">
                    <svg className="empty-execution-icon" viewBox="0 0 64 64" aria-hidden="true">
                      <circle cx="32" cy="32" r="29" />
                      <path d="M25 19v26l20-13-20-13Z" />
                    </svg>
                    <h2>No executions yet</h2>
                    <p>Submit a training job to see its attempts here.</p>
                  </div>
                </div>
              ) : <table className="projects-table">
                <thead>
                  <tr>
                    <th className="col-check"><input ref={selectAll} type="checkbox" aria-label="Select all executions" checked={allSelected} disabled={!visible.length} onChange={(event) => toggleSelection(visible.map((execution) => execution.id), event.target.checked)} /></th>
                    <th>Execution</th><th>Project</th><th>Entrypoint</th><th>State</th><th>GPU</th><th>Created</th><th>Finished</th><th>Result</th>
                  </tr>
                </thead>
                <tbody>
                  {loading ? <tr><td colSpan={9} role="status">Loading executions…</td></tr>
                    : error ? <tr><td colSpan={9}><span role="alert">{error}</span> <button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</button></td></tr>
                      : visible.length === 0 ? <tr><td colSpan={9} role="status">No matching executions.</td></tr>
                        : visible.map((execution) => (
                          <tr key={execution.id}>
                            <td><input type="checkbox" aria-label={`Select execution ${execution.id}`} checked={selected.has(execution.id)} onChange={(event) => toggleSelection([execution.id], event.target.checked)} /></td>
                            <td className="name" title={execution.id}>{execution.id.slice(0, 8)}</td>
                            <td title={execution.project_name}>{execution.project_name}</td>
                            <td className="repo" title={execution.entrypoint}>{execution.entrypoint}</td>
                            <td>{STATE_LABELS[execution.state] || execution.state}</td>
                            <td title={execution.assigned_gpu}>{execution.assigned_gpu ? `${execution.assigned_gpu.slice(0, 12)}${execution.assigned_gpu.length > 12 ? '…' : ''}` : '-'}</td>
                            <td title={execution.created_at}>{timeAgo(execution.created_at)}</td>
                            <td title={execution.finished_at || undefined}>{timeAgo(execution.finished_at)}</td>
                            <td className="repo" title={execution.error || undefined}>{execution.error || (execution.exit_code == null ? '-' : `Exit ${execution.exit_code}`)}</td>
                          </tr>
                        ))}
                </tbody>
              </table>}
            </section>
          </main>
        </div>
      </div>
    </>
  );
}
