"use client";

import React, { useState, useEffect, useCallback } from "react";
import { apiUrl, getAuthHeaders } from "../lib/api";

interface Collection {
  name: string;
  file_count: number;
  total_bytes: number;
  path: string;
  created_at: string;
}

interface StorageUsage {
  used_bytes: number;
  max_bytes: number;
  used_pct: number;
}

interface CollectionBrowserProps {
  isOpen: boolean;
  onClose: () => void;
  onSelectPath: (path: string) => void;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function formatDate(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.floor(diffHours / 24);
  if (diffDays < 30) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

export default function CollectionBrowser({
  isOpen,
  onClose,
  onSelectPath,
}: CollectionBrowserProps) {
  const [collections, setCollections] = useState<Collection[]>([]);
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selecting, setSelecting] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  const fetchCollections = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [colResp, usageResp] = await Promise.all([
        fetch(apiUrl("/api/v1/files/collections"), { headers: { ...getAuthHeaders() } }),
        fetch(apiUrl("/api/v1/files/usage"), { headers: { ...getAuthHeaders() } }),
      ]);
      const colData = await colResp.json();
      const usageData = await usageResp.json();

      if (colData.success) {
        setCollections(colData.data);
      } else {
        setError(colData.detail || "Failed to load collections");
      }
      if (usageData.success) {
        setUsage(usageData.data);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load collections");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen) {
      fetchCollections();
    }
  }, [isOpen, fetchCollections]);

  const handleSelect = async (name: string) => {
    setSelecting(name);
    try {
      const resp = await fetch(apiUrl(`/api/v1/files/collections/${name}/path`), { headers: { ...getAuthHeaders() } });
      const data = await resp.json();
      if (data.success) {
        onSelectPath(data.data.path);
        onClose();
      } else {
        setError(data.detail || "Failed to get collection path");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to get collection path");
    } finally {
      setSelecting(null);
    }
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`Are you sure you want to delete collection '${name}'? This will remove all files.`)) {
      return;
    }
    setDeleting(name);
    try {
      const resp = await fetch(apiUrl(`/api/v1/files/collections/${name}`), {
        method: "DELETE",
        headers: { ...getAuthHeaders() },
      });
      const data = await resp.json();
      if (data.success) {
        await fetchCollections();
      } else {
        setError(data.detail || "Failed to delete collection");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete collection");
    } finally {
      setDeleting(null);
    }
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl w-full max-w-2xl mx-4 max-h-[80vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
            Browse Collections
          </h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
          {/* Loading */}
          {loading && (
            <div className="flex items-center justify-center py-12">
              <svg className="animate-spin h-6 w-6 text-blue-500" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <span className="ml-2 text-sm text-gray-500 dark:text-gray-400">Loading collections...</span>
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="rounded-md bg-red-50 dark:bg-red-900/20 p-4">
              <p className="text-sm text-red-800 dark:text-red-300">{error}</p>
            </div>
          )}

          {/* Empty State */}
          {!loading && !error && collections.length === 0 && (
            <div className="flex flex-col items-center justify-center py-12 text-gray-400 dark:text-gray-500">
              <svg className="h-12 w-12 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
              </svg>
              <p className="text-sm font-medium">No uploaded collections</p>
            </div>
          )}

          {/* Collection List */}
          {!loading && collections.map((col) => (
            <div
              key={col.name}
              className="flex items-center justify-between p-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-700/50 hover:border-gray-300 dark:hover:border-gray-600 transition-colors"
            >
              <div className="flex-1 min-w-0 mr-3">
                <p className="text-sm font-semibold text-gray-900 dark:text-white truncate">
                  {col.name}
                </p>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  {col.file_count} file{col.file_count !== 1 ? "s" : ""} · {formatBytes(col.total_bytes)}
                </p>
                <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">
                  Created {formatDate(col.created_at)}
                </p>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  onClick={() => handleSelect(col.name)}
                  disabled={selecting === col.name}
                  className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {selecting === col.name ? "..." : "Select"}
                </button>
                <button
                  onClick={() => handleDelete(col.name)}
                  disabled={deleting === col.name}
                  className="p-1.5 text-gray-400 hover:text-red-500 dark:hover:text-red-400 disabled:opacity-50 disabled:cursor-not-allowed"
                  title="Delete collection"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                  </svg>
                </button>
              </div>
            </div>
          ))}
        </div>

        {/* Footer — Storage Quota */}
        <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          {usage ? (
            <div>
              <div className="flex justify-between text-xs text-gray-500 dark:text-gray-400 mb-1">
                <span>Storage: {formatBytes(usage.used_bytes)} / {formatBytes(usage.max_bytes)} used</span>
                <span>{(usage.used_pct * 100).toFixed(1)}%</span>
              </div>
              <div className="w-full h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all ${
                    usage.used_pct > 0.9 ? "bg-red-500" : usage.used_pct > 0.7 ? "bg-yellow-500" : "bg-blue-500"
                  }`}
                  style={{ width: `${Math.min(usage.used_pct * 100, 100)}%` }}
                />
              </div>
            </div>
          ) : (
            <div className="h-6" />
          )}
        </div>
      </div>
    </div>
  );
}
