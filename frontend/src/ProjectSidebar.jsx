import React, { useEffect, useRef, useState } from 'react';
import { listProjectFiles, readProjectFile } from './api.js';
import TrainingForm from './TrainingForm.jsx';

function Directory({ projectId, path = '', selectedFile, onSelectFile }) {
  const [entries, setEntries] = useState(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setEntries(null);
    setError('');
    listProjectFiles(projectId, path, controller.signal)
      .then((data) => { if (!controller.signal.aborted) setEntries(data.entries); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message); });
    return () => controller.abort();
  }, [projectId, path, attempt]);

  if (error) return <div className="tree-message"><span role="alert">{error}</span> <button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</button></div>;
  if (entries === null) return <p className="tree-message" role="status">Loading files…</p>;
  if (!entries.length) return <p className="tree-message">Empty directory</p>;
  return <ul className="directory-list">
    {entries.map((entry) => <DirectoryEntry key={entry.path} projectId={projectId} entry={entry} selectedFile={selectedFile} onSelectFile={onSelectFile} />)}
  </ul>;
}

function DirectoryEntry({ projectId, entry, selectedFile, onSelectFile }) {
  const [expanded, setExpanded] = useState(false);
  const isDirectory = entry.type === 'directory';
  return <li>
    {isDirectory ? <>
      <button className="directory-toggle" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)} title={entry.path}>
        <span aria-hidden="true">{expanded ? '▾' : '▸'}</span>
        <span className="entry-name">{entry.name}</span>
      </button>
      {expanded && <Directory projectId={projectId} path={entry.path} selectedFile={selectedFile} onSelectFile={onSelectFile} />}
    </> : <button type="button" className="directory-file" title={entry.path} aria-pressed={selectedFile === entry.path} onClick={() => onSelectFile(entry.path)} disabled={entry.type !== 'file'}>
      <span aria-hidden="true">{entry.type === 'symlink' ? '↗' : entry.type === 'submodule' ? '▣' : '·'}</span>
      <span className="entry-name">{entry.name}</span>
      {entry.type !== 'file' && <span className="entry-type">{entry.type}</span>}
    </button>}
  </li>;
}

function FilePreview({ projectId, path }) {
  const [content, setContent] = useState(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setContent(null);
    setError('');
    readProjectFile(projectId, path, controller.signal)
      .then((data) => { if (!controller.signal.aborted) setContent(data.content); })
      .catch((failure) => { if (!controller.signal.aborted) setError(failure.message); });
    return () => controller.abort();
  }, [projectId, path, attempt]);
  if (error) return <div className="tree-message"><span role="alert">{error}</span> <button type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</button></div>;
  if (content === null) return <p className="tree-message" role="status">Loading file…</p>;
  if (!content) return <p className="tree-message">This file is empty.</p>;
  return <pre className="file-content"><code>{content}</code></pre>;
}

export default function ProjectSidebar({ project, onClose }) {
  const [selectedFile, setSelectedFile] = useState(null);
  const closeButton = useRef(null);
  useEffect(() => { closeButton.current?.focus(); }, []);

  return <aside id="project-sidebar" className="project-sidebar" aria-labelledby="project-sidebar-title" onKeyDown={(event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      onClose();
    }
  }}>
    <section className="project-tree-section" aria-label="Project directory">
      <header className="project-sidebar-header">
        <div>
          <h2 id="project-sidebar-title">{project.name}</h2>
          <p>Project directory</p>
        </div>
        <button ref={closeButton} className="sidebar-close" type="button" aria-label="Close project sidebar" onClick={onClose}>×</button>
      </header>
      <div className="project-tree"><Directory projectId={project.id} selectedFile={selectedFile} onSelectFile={setSelectedFile} /></div>
    </section>
    <section className="project-file-preview" aria-label="File contents">
      <TrainingForm projectId={project.id} path={selectedFile} />
      {selectedFile
        ? <FilePreview key={selectedFile} projectId={project.id} path={selectedFile} />
        : <p className="tree-message">Select a file above to view its contents.</p>}
    </section>
  </aside>;
}
