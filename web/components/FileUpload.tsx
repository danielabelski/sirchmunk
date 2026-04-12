"use client";

import React, { useState, useRef, useCallback } from "react";

// File System Access API types for drag-and-drop folder support
interface FileSystemEntry {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  fullPath: string;
}
interface FileSystemFileEntry extends FileSystemEntry {
  file(successCallback: (file: File) => void, errorCallback?: (error: Error) => void): void;
}
interface FileSystemDirectoryEntry extends FileSystemEntry {
  createReader(): FileSystemDirectoryReader;
}
interface FileSystemDirectoryReader {
  readEntries(successCallback: (entries: FileSystemEntry[]) => void, errorCallback?: (error: Error) => void): void;
}

interface FileWithPath {
  file: File;
  relativePath: string; // e.g. "src/data/file.pdf" or just "file.pdf" for flat files
}

async function readAllEntries(entries: FileSystemEntry[]): Promise<FileWithPath[]> {
  const files: FileWithPath[] = [];

  async function processEntry(entry: FileSystemEntry): Promise<void> {
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) => {
        (entry as FileSystemFileEntry).file(resolve, reject);
      });
      const relativePath = entry.fullPath.replace(/^\//, '') || file.name;
      files.push({ file, relativePath });
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader();
      let batch: FileSystemEntry[];
      do {
        batch = await new Promise<FileSystemEntry[]>((resolve, reject) => {
          reader.readEntries(resolve, reject);
        });
        for (const child of batch) {
          await processEntry(child);
        }
      } while (batch.length > 0);
    }
  }

  for (const entry of entries) {
    await processEntry(entry);
  }
  return files;
}
import { apiUrl, getAuthHeaders } from "../lib/api";

interface UploadedFile {
  file_id: string;
  name: string;
  size_bytes: number;
  sha256: string;
  upload_time: string;
  collection: string;
}

interface UploadError {
  name: string;
  error: string;
}

interface UploadResult {
  collection: string;
  collection_path: string | null;
  uploaded: UploadedFile[];
  errors: UploadError[];
  total_uploaded: number;
  total_errors: number;
}

interface FileUploadProps {
  isOpen: boolean;
  onClose: () => void;
  onUploadComplete?: (collectionPath: string) => void;
}

export default function FileUpload({
  isOpen,
  onClose,
  onUploadComplete,
}: FileUploadProps) {
  const [collection, setCollection] = useState("default");
  const [selectedFiles, setSelectedFiles] = useState<FileWithPath[]>([]);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isDragOver, setIsDragOver] = useState(false);
  const [dupCheckLoading, setDupCheckLoading] = useState(false);
  const [duplicateFiles, setDuplicateFiles] = useState<string[]>([]);
  const [showDupDialog, setShowDupDialog] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  const addFilesDeduped = useCallback(
    (newFiles: FileWithPath[]) => {
      setSelectedFiles((prev) => {
        const existing = new Set(prev.map((f) => f.relativePath));
        const unique = newFiles.filter((f) => !existing.has(f.relativePath));
        return [...prev, ...unique];
      });
    },
    [],
  );

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    setResult(null);
    setError(null);

    // Extract entries synchronously before the event is garbage-collected
    const items = Array.from(e.dataTransfer.items);
    const entries: FileSystemEntry[] = [];
    for (const item of items) {
      const entry = (item as any).webkitGetAsEntry?.() as FileSystemEntry | null;
      if (entry) entries.push(entry);
    }

    if (entries.length > 0) {
      readAllEntries(entries).then((filesWithPaths) => {
        if (filesWithPaths.length > 0) {
          addFilesDeduped(filesWithPaths);
        }
      });
    } else {
      // Fallback: use dataTransfer.files directly (flat files only)
      const droppedFiles = Array.from(e.dataTransfer.files);
      if (droppedFiles.length > 0) {
        const filesWithPaths = droppedFiles.map((f) => ({
          file: f,
          relativePath: f.name,
        }));
        addFilesDeduped(filesWithPaths);
      }
    }
  }, [addFilesDeduped]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      const filesWithPaths = Array.from(e.target.files).map((f) => ({
        file: f,
        relativePath: f.name,
      }));
      addFilesDeduped(filesWithPaths);
      setResult(null);
      setError(null);
    }
  };

  const handleFolderSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const fileList = e.target.files;
    if (!fileList) return;

    const newFiles: FileWithPath[] = [];
    for (let i = 0; i < fileList.length; i++) {
      const file = fileList[i];
      const relativePath = file.webkitRelativePath || file.name;
      newFiles.push({ file, relativePath });
    }

    addFilesDeduped(newFiles);
    setResult(null);
    setError(null);
    e.target.value = "";
  };


  const removeFile = (index: number) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const formatSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024)
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  };

  const checkDuplicates = async (): Promise<{duplicates: any[], new_files: string[]} | null> => {
    const fileEntries = selectedFiles.map(f => ({
      name: f.relativePath,
      size: f.file.size,
    }));

    try {
      const res = await fetch(apiUrl("/api/v1/files/check-duplicates"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...getAuthHeaders(),
        },
        body: JSON.stringify({
          collection: collection,
          files: fileEntries,
        }),
      });

      if (res.ok) {
        return await res.json();
      }
      return null;
    } catch (e) {
      return null;
    }
  };

  const handleUploadClick = async () => {
    setDupCheckLoading(true);
    const result = await checkDuplicates();
    setDupCheckLoading(false);

    if (!result || result.duplicates.length === 0) {
      // No duplicates — proceed with upload
      handleUpload(false);
      return;
    }

    // Duplicates found — show confirmation dialog
    setDuplicateFiles(result.duplicates.map((d: any) => d.name));
    setShowDupDialog(true);
  };

  const handleUpload = async (overwrite: boolean, skipFiles?: string[]) => {
    const filesToUpload = skipFiles
      ? selectedFiles.filter(f => !skipFiles.includes(f.relativePath))
      : selectedFiles;

    if (filesToUpload.length === 0) {
      setError("No new files to upload");
      return;
    }

    setUploading(true);
    setProgress(0);
    setError(null);
    setResult(null);

    const formData = new FormData();
    formData.append("collection", collection);
    if (overwrite) {
      formData.append("overwrite", "true");
    }
    filesToUpload.forEach(({ file, relativePath }) => {
      formData.append("files", file);
      formData.append("paths", relativePath);
    });

    try {
      const resp = await fetch(apiUrl("/api/v1/files/upload"), {
        method: "POST",
        headers: { ...getAuthHeaders() },
        body: formData,
      });

      const data = await resp.json();

      if (data.success) {
        setResult(data.data);
        setSelectedFiles([]);
        if (data.data.collection_path && onUploadComplete) {
          onUploadComplete(data.data.collection_path);
        }
      } else {
        setError(data.detail || "Upload failed");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
      setProgress(100);
    }
  };

  const handleClose = () => {
    setSelectedFiles([]);
    setResult(null);
    setError(null);
    setUploading(false);
    onClose();
  };

  if (!isOpen) return null;

  const totalSize = selectedFiles.reduce((sum, f) => sum + f.file.size, 0);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-2xl mx-4 max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            Upload Files
          </h2>
          <button
            onClick={handleClose}
            className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {/* Collection Name */}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Collection Name
            </label>
            <input
              type="text"
              value={collection}
              onChange={(e) => setCollection(e.target.value)}
              placeholder="default"
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-md bg-white dark:bg-gray-700 text-gray-900 dark:text-white text-sm focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              pattern="[a-zA-Z0-9][a-zA-Z0-9_-]*"
              maxLength={64}
            />
            <p className="mt-1 text-xs text-gray-500">
              Alphanumeric, dashes, underscores. Used as search path identifier.
            </p>
          </div>

          {/* Drop Zone */}
          <div
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDragEnter={(e) => e.preventDefault()}
            onDrop={handleDrop}
            className={`border-2 border-dashed rounded-lg py-8 px-4 text-center transition-colors flex flex-col items-center justify-center ${
              isDragOver
                ? "border-blue-400 bg-gray-700/50"
                : "border-gray-600"
            }`}
          >
            <svg className="mx-auto h-10 w-10 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
            </svg>
            <p className="text-gray-300 text-sm mt-2">
              Drag &amp; drop files or folders here
            </p>
            <p className="text-gray-500 text-xs mt-1">
              Max 1 GB per file · 10 GB total
            </p>
            <div className="flex gap-3 mt-3">
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  fileInputRef.current?.click();
                }}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded-lg transition-colors flex items-center gap-2"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
                </svg>
                Select Files
              </button>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  folderInputRef.current?.click();
                }}
                className="px-4 py-2 bg-gray-600 hover:bg-gray-500 text-white text-sm rounded-lg transition-colors flex items-center gap-2"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                </svg>
                Select Folder
              </button>
            </div>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              onChange={handleFileSelect}
              style={{ display: 'none' }}
              accept=".pdf,.docx,.doc,.xlsx,.xls,.pptx,.txt,.md,.csv,.json,.html,.xml,.rtf,.epub,.yaml,.yml,.log,.tsv"
            />
            <input
              type="file"
              ref={folderInputRef}
              className="hidden"
              // @ts-ignore
              webkitdirectory=""
              directory=""
              multiple
              onChange={handleFolderSelect}
            />
          </div>

          {/* Selected Files List */}
          {selectedFiles.length > 0 && (
            <div>
              <div className="flex justify-between items-center mb-2">
                <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  {selectedFiles.length} file(s) selected ({formatSize(totalSize)})
                </span>
                <button
                  onClick={() => setSelectedFiles([])}
                  className="text-xs text-red-500 hover:text-red-700"
                >
                  Clear all
                </button>
              </div>
              <div className="max-h-40 overflow-y-auto space-y-1">
                {selectedFiles.map(({ file, relativePath }, idx) => (
                  <div
                    key={`${relativePath}-${idx}`}
                    className="flex items-center justify-between py-1 px-2 rounded bg-gray-50 dark:bg-gray-700/50 text-sm"
                  >
                    <span className="truncate text-gray-700 dark:text-gray-300 flex-1 mr-2" title={relativePath}>
                      {relativePath}
                    </span>
                    <span className="text-gray-500 text-xs whitespace-nowrap mr-2">
                      {formatSize(file.size)}
                    </span>
                    <button
                      onClick={() => removeFile(idx)}
                      className="text-gray-400 hover:text-red-500"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Upload Result */}
          {result && (
            <div className="rounded-md bg-green-50 dark:bg-green-900/20 p-4">
              <p className="text-sm font-medium text-green-800 dark:text-green-300">
                Upload complete: {result.total_uploaded} file(s) uploaded
                {result.total_errors > 0 && `, ${result.total_errors} error(s)`}
              </p>
              {result.collection_path && (
                <p className="mt-1 text-xs text-green-700 dark:text-green-400">
                  Collection path: <code className="bg-green-100 dark:bg-green-800 px-1 rounded">{result.collection_path}</code>
                </p>
              )}
              {result.errors.length > 0 && (
                <div className="mt-2">
                  {result.errors.map((err, i) => (
                    <p key={i} className="text-xs text-red-600 dark:text-red-400">
                      {err.name}: {err.error}
                    </p>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-4">
              <p className="text-sm text-red-800 dark:text-red-300">{error}</p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          <button
            onClick={handleClose}
            className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-700 rounded-md hover:bg-gray-200 dark:hover:bg-gray-600"
          >
            Close
          </button>
          <button
            onClick={handleUploadClick}
            disabled={uploading || dupCheckLoading || selectedFiles.length === 0}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {dupCheckLoading ? "Checking..." : uploading ? "Uploading..." : `Upload ${selectedFiles.length > 0 ? `(${selectedFiles.length})` : ""}`}
          </button>
        </div>
      </div>

      {showDupDialog && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-gray-800 border border-gray-600 rounded-lg p-6 max-w-md w-full mx-4 shadow-xl">
            <h3 className="text-white text-lg font-semibold mb-3">
              Duplicate Files Found
            </h3>
            <p className="text-gray-300 text-sm mb-3">
              {duplicateFiles.length} file{duplicateFiles.length > 1 ? "s" : ""} already exist{duplicateFiles.length === 1 ? "s" : ""} in collection &ldquo;{collection}&rdquo;:
            </p>
            <div className="max-h-40 overflow-y-auto bg-gray-900 rounded p-2 mb-4">
              {duplicateFiles.map((name, i) => (
                <p key={i} className="text-gray-400 text-xs py-0.5 truncate" title={name}>
                  {name}
                </p>
              ))}
            </div>
            <div className="flex gap-2 justify-end">
              <button
                onClick={() => {
                  setShowDupDialog(false);
                  setDuplicateFiles([]);
                }}
                className="px-3 py-1.5 text-sm text-gray-300 hover:text-white bg-gray-700 hover:bg-gray-600 rounded transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  setShowDupDialog(false);
                  handleUpload(false, duplicateFiles);
                  setDuplicateFiles([]);
                }}
                className="px-3 py-1.5 text-sm text-white bg-blue-600 hover:bg-blue-500 rounded transition-colors"
              >
                Skip Duplicates
              </button>
              <button
                onClick={() => {
                  setShowDupDialog(false);
                  handleUpload(true);
                  setDuplicateFiles([]);
                }}
                className="px-3 py-1.5 text-sm text-white bg-orange-600 hover:bg-orange-500 rounded transition-colors"
              >
                Overwrite All
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
