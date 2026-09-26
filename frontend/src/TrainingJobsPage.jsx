import React, { useEffect, useRef, useState } from 'react';
import { confirmTraining, listTrainingJobs } from './api.js';
import '../styles.css';

function importedAt(value) {
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

const STATUS_LABELS = {
  checked: 'Ready to submit',
  queued: 'Queued',
  building: 'Building image',
  running: 'Running',
  finished: 'Finished',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

export default function TrainingJobsPage({ onNavigate }) {
  const [jobs, setJobs] = useState([]);
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
    listTrainingJobs(controller.signal)
      .then((data) => { if (!controller.signal.aborted) setJobs(data); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [attempt]);

  const visible = jobs.filter((job) => {
    const term = query.trim().toLowerCase();
    if (!term) return true;
    return job.project_name.toLowerCase().includes(term)
      || job.entrypoint.toLowerCase().includes(term);
  });
  const showEmptyState = !loading && !error && visible.length === 0 && !query.trim();
  const allSelected = visible.length > 0 && visible.every((job) => selected.has(job.id));
  const someSelected = visible.some((job) => selected.has(job.id));

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
            <a className="nav-item active" href="#" aria-current="page"><span className="nav-icon" aria-hidden="true">◉</span>Training Jobs</a>
            <a className="nav-item" href="#"><span className="nav-icon" aria-hidden="true">▶</span>Executions</a>
            <a className="nav-item" href="#"><span className="nav-icon" aria-hidden="true">⚙</span>Settings</a>
          </nav>
        </aside>
        <div className="content">
          <main className="dashboard">
            <div className="page-head">
              <h1>Training Jobs</h1>
            </div>
            <section aria-label="Training jobs" aria-busy={loading}>
              {!showEmptyState && <input className="project-search" type="search" aria-label="Search training jobs" placeholder="Search training jobs" value={query} onChange={(event) => setQuery(event.target.value)} />}
              {showEmptyState ? (
                <div className="projects-empty-state" role="status">
                  <div className="empty-state-content">
                    <svg className="empty-folder-icon" viewBox="0 0 64 52" aria-hidden="true">
                      <path d="M3 11.5A5.5 5.5 0 0 1 8.5 6h17l5 6h25A5.5 5.5 0 0 1 61 17.5v25a5.5 5.5 0 0 1-5.5 5.5h-47A5.5 5.5 0 0 1 3 42.5v-31Z" />
                    </svg>
                    <h2>No training jobs yet</h2>
                    <p>Submit a training run from a project to begin.</p>
                  </div>
                </div>
              ) : <table className="projects-table">
                <thead>
                  <tr>
                    <th className="col-check"><input ref={selectAll} type="checkbox" aria-label="Select all training jobs" checked={allSelected} disabled={!visible.length} onChange={(event) => toggleSelection(visible.map((job) => job.id), event.target.checked)} /></th>
                    <th>Project</th><th>Entrypoint</th><th>Status</th><th>GPU</th><th>Dataset</th><th>Created</th><th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {loading ? <tr><td colSpan={8} role="status">Loading training jobs…</td></tr>
                    : error ? <tr><td colSpan={8}><span role="alert">{error}</span> <button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</button></td></tr>
                      : visible.length === 0 ? <tr><td colSpan={8} role="status">{query.trim() ? 'No matching training jobs.' : 'No training jobs yet.'}</td></tr>
                        : visible.map((job) => (
                          <tr key={job.id} className="project-row">
                            <td><input type="checkbox" aria-label={`Select ${job.entrypoint}`} checked={selected.has(job.id)} onChange={(event) => toggleSelection([job.id], event.target.checked)} /></td>
                            <td className="name" title={job.project_name}>{job.project_name}</td>
                            <td className="repo" title={job.entrypoint}>{job.entrypoint}</td>
                            <td title={job.error}>{job.startup_check ? 'Check: ' : ''}{STATUS_LABELS[job.status] || job.status}</td>
                            <td>{job.requested_gpu}</td>
                            <td title={job.dataset?.name}>{job.dataset?.name || '-'}</td>
                            <td title={job.created_at}>{importedAt(job.created_at)}</td>
                            <td>{job.startup_check && job.status === 'checked' ? <button type="button" onClick={async (event) => {
                              const button = event.currentTarget;
                              button.disabled = true;
                              try { await confirmTraining(job.project_id, job.id); setAttempt((value) => value + 1); }
                              catch (failure) { setError(failure.message); button.disabled = false; }
                            }}>Submit training</button> : '-'}</td>
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
