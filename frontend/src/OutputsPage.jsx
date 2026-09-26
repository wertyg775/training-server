import React, { useEffect, useRef, useState } from 'react';
import { listOutputRuns, outputDownloadUrl } from './api.js';
import '../styles.css';

const STATE_LABELS = {
  building: 'Building image',
  starting: 'Starting',
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '-' : new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date);
}

function fileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  const power = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length);
  return `${(bytes / 1024 ** power).toFixed(1)} ${units[power - 1]}`;
}

function fileTree(files) {
  const root = { folders: new Map(), files: [] };
  for (const file of files) {
    const parts = file.path.split('/');
    let folder = root;
    for (const name of parts.slice(0, -1)) {
      if (!folder.folders.has(name)) folder.folders.set(name, { folders: new Map(), files: [] });
      folder = folder.folders.get(name);
    }
    folder.files.push({ ...file, name: parts.at(-1) });
  }
  return root;
}

function OutputTree({ folder, executionId }) {
  return (
    <ul className="output-tree">
      {[...folder.folders.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([name, child]) => (
        <li key={name}>
          <details open>
            <summary><span aria-hidden="true">▣</span> {name}</summary>
            <OutputTree folder={child} executionId={executionId} />
          </details>
        </li>
      ))}
      {folder.files.sort((left, right) => left.name.localeCompare(right.name)).map((file) => (
        <li key={file.path} className="output-file">
          <a href={outputDownloadUrl(executionId, file.path)} download={file.name} aria-label={`Download ${file.path}`} title={file.path}>
            <span aria-hidden="true">↓</span> <span>{file.name}</span>
          </a>
          <span className="output-file-size">{fileSize(file.size_bytes)}</span>
        </li>
      ))}
    </ul>
  );
}

function OutputSidebar({ run, onClose }) {
  const closeButton = useRef(null);
  useEffect(() => {
    closeButton.current?.focus();
    const closeOnEscape = (event) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose]);
  return (
    <aside id="output-sidebar" className="output-sidebar" aria-labelledby="output-sidebar-title">
      <header className="output-sidebar-header">
        <div>
          <h2 id="output-sidebar-title">{run.project_name}</h2>
          <p>{run.entrypoint} · Attempt {run.run_number}</p>
          <p>{dateLabel(run.created_at)} · {STATE_LABELS[run.state] || run.state}</p>
        </div>
        <button ref={closeButton} className="sidebar-close" type="button" aria-label="Close outputs" onClick={onClose}>×</button>
      </header>
      <div className="output-sidebar-body">
        <h3>Saved files</h3>
        {run.truncated && <p className="output-limit-note">This run has more files than can be shown here.</p>}
        <OutputTree folder={fileTree(run.files)} executionId={run.execution_id} />
      </div>
    </aside>
  );
}

export default function OutputsPage({ onNavigate }) {
  const [runs, setRuns] = useState([]);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [activeRunId, setActiveRunId] = useState(null);
  const selectAll = useRef(null);
  const opener = useRef(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    listOutputRuns(controller.signal)
      .then((data) => { if (!controller.signal.aborted) setRuns(data); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [attempt]);

  const term = query.trim().toLowerCase();
  const visible = runs.filter((run) => !term
    || run.project_name.toLowerCase().includes(term)
    || run.entrypoint.toLowerCase().includes(term)
    || (STATE_LABELS[run.state] || run.state).toLowerCase().includes(term)
    || run.files.some((file) => file.path.toLowerCase().includes(term)));
  const showEmptyState = !loading && !error && visible.length === 0 && !term;
  const allSelected = visible.length > 0 && visible.every((run) => selected.has(run.execution_id));
  const someSelected = visible.some((run) => selected.has(run.execution_id));
  const activeRun = runs.find((run) => run.execution_id === activeRunId);

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

  function closeSidebar() {
    setActiveRunId(null);
    opener.current?.focus();
  }

  return (
    <>
      <header className="brand">Training Server</header>
      <div className="layout">
        <aside className="sidebar">
          <nav className="nav" aria-label="Main">
            <a className="nav-item" href="#" onClick={(event) => { event.preventDefault(); onNavigate('projects'); }}><span className="nav-icon" aria-hidden="true">▣</span>Projects</a>
            <a className="nav-item" href="#" onClick={(event) => { event.preventDefault(); onNavigate('training-jobs'); }}><span className="nav-icon" aria-hidden="true">◉</span>Training Jobs</a>
            <a className="nav-item" href="#" onClick={(event) => { event.preventDefault(); onNavigate('executions'); }}><span className="nav-icon" aria-hidden="true">▶</span>Executions</a>
            <a className="nav-item active" href="#" aria-current="page"><span className="nav-icon" aria-hidden="true">▤</span>Outputs</a>
            <a className="nav-item" href="#"><span className="nav-icon" aria-hidden="true">⚙</span>Settings</a>
          </nav>
        </aside>
        <div className="content">
          <main className="dashboard">
            <div className="page-head"><h1>Outputs</h1></div>
            <section aria-label="Saved outputs" aria-busy={loading}>
              {!showEmptyState && <input className="project-search" type="search" aria-label="Search outputs" placeholder="Search projects, scripts, or files" value={query} onChange={(event) => setQuery(event.target.value)} />}
              {showEmptyState ? (
                <div className="projects-empty-state" role="status">
                  <div className="empty-state-content">
                    <svg className="empty-execution-icon" viewBox="0 0 64 64" aria-hidden="true">
                      <path d="M6 17a5 5 0 0 1 5-5h15l6 7h21a5 5 0 0 1 5 5v27a5 5 0 0 1-5 5H11a5 5 0 0 1-5-5V17Z" />
                    </svg>
                    <h2>No outputs yet</h2>
                    <p>Files saved to /output or created in the project folder during training will appear here.</p>
                  </div>
                </div>
              ) : <table className="projects-table">
                <thead>
                  <tr>
                    <th className="col-check"><input ref={selectAll} type="checkbox" aria-label="Select all output runs" checked={allSelected} disabled={!visible.length} onChange={(event) => toggleSelection(visible.map((run) => run.execution_id), event.target.checked)} /></th>
                    <th>Project</th><th>Script</th><th>Run</th><th>State</th><th>Files</th><th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {loading ? <tr><td colSpan={7} role="status">Loading outputs…</td></tr>
                    : error ? <tr><td colSpan={7}><span role="alert">{error}</span> <button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</button></td></tr>
                      : visible.length === 0 ? <tr><td colSpan={7} role="status">No matching outputs.</td></tr>
                        : visible.map((run) => (
                          <tr key={run.execution_id} className={activeRunId === run.execution_id ? 'project-row-active' : ''}>
                            <td><input type="checkbox" aria-label={`Select outputs from ${run.project_name}, attempt ${run.run_number}`} checked={selected.has(run.execution_id)} onChange={(event) => toggleSelection([run.execution_id], event.target.checked)} /></td>
                            <td className="name" title={run.project_name}>{run.project_name}</td>
                            <td className="repo" title={run.entrypoint}>{run.entrypoint}</td>
                            <td title={dateLabel(run.created_at)}>Attempt {run.run_number} · {dateLabel(run.created_at)}</td>
                            <td>{STATE_LABELS[run.state] || run.state}</td>
                            <td>{run.files.length}{run.truncated ? '+' : ''} {run.files.length === 1 && !run.truncated ? 'file' : 'files'}</td>
                            <td><button type="button" aria-expanded={activeRunId === run.execution_id} aria-controls={activeRun ? 'output-sidebar' : undefined} onClick={(event) => { opener.current = event.currentTarget; setActiveRunId(run.execution_id); }}>Browse</button></td>
                          </tr>
                        ))}
                </tbody>
              </table>}
            </section>
          </main>
        </div>
      </div>
      {activeRun && <OutputSidebar key={activeRun.execution_id} run={activeRun} onClose={closeSidebar} />}
    </>
  );
}
