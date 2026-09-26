import React, { useEffect, useRef, useState } from 'react';
import { uploadDataset } from './api.js';

export default function DatasetModal({ projectId, onUploaded, onClose }) {
  const dialog = useRef(null);
  const uploading = useRef(false);
  const [file, setFile] = useState(null);
  const [target, setTarget] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const element = dialog.current;
    element.showModal();
    return () => element.close();
  }, []);

  async function submit(event) {
    event.preventDefault();
    if (!file || uploading.current) return;
    uploading.current = true;
    setPending(true);
    setError('');
    try {
      const dataset = await uploadDataset(projectId, file);
      onUploaded({ ...dataset, target: target.trim() });
      onClose();
    } catch (failure) {
      setError(failure.message);
    } finally {
      uploading.current = false;
      setPending(false);
    }
  }

  return <dialog ref={dialog} className="upload-modal dataset-modal" aria-labelledby="dataset-title"
    onCancel={(event) => { event.preventDefault(); if (!pending) onClose(); }}
    onKeyDown={(event) => event.stopPropagation()}
    onClick={(event) => {
      const bounds = event.currentTarget.getBoundingClientRect();
      if (!pending && event.target === event.currentTarget && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) onClose();
    }}>
    <button className="modal-close" type="button" aria-label="Close dataset upload" disabled={pending} onClick={onClose}>×</button>
    <h2 id="dataset-title">Upload dataset</h2>
    <form onSubmit={submit}>
      <label className="modal-label" htmlFor="dataset-file">File or ZIP</label>
      <input id="dataset-file" type="file" required disabled={pending} onChange={(event) => {
        const selected = event.target.files[0] || null;
        setFile(selected);
        setTarget(selected ? (selected.name.toLowerCase().endsWith('.zip') ? 'data' : `data/${selected.name}`) : '');
      }} />
      <label className="modal-label" htmlFor="dataset-location">{file?.name.toLowerCase().endsWith('.zip') ? 'Folder in project' : 'File path in project'}</label>
      <input id="dataset-location" className="project-search modal-search" required value={target} placeholder="data/train.csv" disabled={pending} onChange={(event) => setTarget(event.target.value)} />
      {error && <p role="alert">{error}</p>}
      <button className="modal-confirm" type="submit" disabled={pending || !file || !target.trim()}>{pending ? 'Uploading…' : 'Upload'}</button>
    </form>
  </dialog>;
}
