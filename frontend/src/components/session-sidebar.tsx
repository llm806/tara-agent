"use client";

import {
  Check,
  LoaderCircle,
  LogOut,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Plus,
  RefreshCw,
  Trash2,
  Waves,
  X,
} from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";

import { deleteSession, deleteSessions, listSessions, updateSession } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { SessionSummary } from "@/lib/types";
import { useAuth } from "@/components/auth-gate";

type SessionSidebarProps = {
  activeSessionId?: string;
  refreshVersion: number;
  disabled: boolean;
  onNewSession: () => void;
  onSelectSession: (sessionId: string) => void;
  onSessionsDeleted: (sessionIds: string[]) => void;
};

type DeleteRequest = {
  sessionIds: string[];
  title?: string;
};

export function SessionSidebar({
  activeSessionId,
  refreshVersion,
  disabled,
  onNewSession,
  onSelectSession,
  onSessionsDeleted,
}: SessionSidebarProps) {
  const { user, signOut } = useAuth();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const [retryVersion, setRetryVersion] = useState(0);
  const [managing, setManaging] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [deleting, setDeleting] = useState(false);
  const [menuSessionId, setMenuSessionId] = useState<string>();
  const [editingSessionId, setEditingSessionId] = useState<string>();
  const [titleDraft, setTitleDraft] = useState("");
  const [updatingSessionId, setUpdatingSessionId] = useState<string>();
  const [deleteRequest, setDeleteRequest] = useState<DeleteRequest>();
  const [deleteError, setDeleteError] = useState<string>();
  const [signOutRequested, setSignOutRequested] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    listSessions(controller.signal)
      .then((response) => {
        setSessions(response.items);
        const availableIds = new Set(response.items.map((session) => session.id));
        setSelectedIds((current) => new Set(
          [...current].filter((sessionId) => availableIds.has(sessionId)),
        ));
        setError(undefined);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") {
          return;
        }
        setError(reason instanceof Error ? reason.message : "无法加载对话记录");
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [refreshVersion, retryVersion]);

  useEffect(() => {
    if (!menuSessionId) {
      return;
    }
    function closeMenu(event: PointerEvent) {
      if (event.target instanceof Node && !menuRef.current?.contains(event.target)) {
        setMenuSessionId(undefined);
      }
    }
    function closeMenuWithEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMenuSessionId(undefined);
      }
    }
    document.addEventListener("pointerdown", closeMenu);
    document.addEventListener("keydown", closeMenuWithEscape);
    return () => {
      document.removeEventListener("pointerdown", closeMenu);
      document.removeEventListener("keydown", closeMenuWithEscape);
    };
  }, [menuSessionId]);

  useEffect(() => {
    if (!deleteRequest) {
      return;
    }
    function closeDialogWithEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && !deleting) {
        setDeleteRequest(undefined);
        setDeleteError(undefined);
      }
    }
    document.addEventListener("keydown", closeDialogWithEscape);
    return () => document.removeEventListener("keydown", closeDialogWithEscape);
  }, [deleteRequest, deleting]);

  useEffect(() => {
    if (!signOutRequested) {
      return;
    }
    function closeDialogWithEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && !signingOut) {
        setSignOutRequested(false);
      }
    }
    document.addEventListener("keydown", closeDialogWithEscape);
    return () => document.removeEventListener("keydown", closeDialogWithEscape);
  }, [signOutRequested, signingOut]);

  async function confirmSignOut() {
    setSigningOut(true);
    try {
      await signOut();
      setSignOutRequested(false);
    } finally {
      setSigningOut(false);
    }
  }

  function toggleManaging() {
    setManaging((current) => !current);
    setSelectedIds(new Set());
    setMenuSessionId(undefined);
    setEditingSessionId(undefined);
  }

  function toggleSession(sessionId: string) {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(sessionId)) {
        next.delete(sessionId);
      } else {
        next.add(sessionId);
      }
      return next;
    });
  }

  function toggleAllSessions() {
    setSelectedIds((current) => (
      current.size === sessions.length
        ? new Set()
        : new Set(sessions.map((session) => session.id))
    ));
  }

  function requestSelectedSessionsDeletion() {
    const sessionIds = [...selectedIds];
    if (sessionIds.length === 0) {
      return;
    }
    const title = sessionIds.length === 1
      ? sessions.find((session) => session.id === sessionIds[0])?.title ?? undefined
      : undefined;
    setDeleteError(undefined);
    setDeleteRequest({ sessionIds, title });
  }

  async function confirmDeletion() {
    if (!deleteRequest) {
      return;
    }
    const sessionIds = deleteRequest.sessionIds;
    setDeleting(true);
    setDeleteError(undefined);
    try {
      let deletedIds: string[];
      if (sessionIds.length === 1) {
        await deleteSession(sessionIds[0]);
        deletedIds = sessionIds;
      } else {
        const response = await deleteSessions(sessionIds);
        deletedIds = response.deleted_ids;
      }
      applyDeletedSessions(deletedIds);
      if (managing) {
        setManaging(false);
      }
      setDeleteRequest(undefined);
    } catch (reason) {
      setDeleteError(reason instanceof Error ? reason.message : "删除会话失败");
    } finally {
      setDeleting(false);
    }
  }

  function applyDeletedSessions(sessionIds: string[]) {
    const deleted = new Set(sessionIds);
    setSessions((current) => current.filter((session) => !deleted.has(session.id)));
    setSelectedIds(new Set());
    setMenuSessionId(undefined);
    onSessionsDeleted(sessionIds);
  }

  function requestSessionDeletion(session: SessionSummary) {
    setMenuSessionId(undefined);
    setDeleteError(undefined);
    setDeleteRequest({
      sessionIds: [session.id],
      title: session.title ?? "未命名对话",
    });
  }

  function startRenaming(session: SessionSummary) {
    setMenuSessionId(undefined);
    setEditingSessionId(session.id);
    setTitleDraft(session.title ?? "");
  }

  async function renameSession(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editingSessionId) {
      return;
    }
    const title = titleDraft.trim();
    if (!title) {
      return;
    }
    setUpdatingSessionId(editingSessionId);
    setError(undefined);
    try {
      const updated = await updateSession(editingSessionId, { title });
      setSessions((current) => sortSessions(
        current.map((session) => session.id === updated.id ? updated : session),
      ));
      setEditingSessionId(undefined);
      setTitleDraft("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重命名失败");
    } finally {
      setUpdatingSessionId(undefined);
    }
  }

  async function togglePinned(session: SessionSummary) {
    setMenuSessionId(undefined);
    setUpdatingSessionId(session.id);
    setError(undefined);
    try {
      const updated = await updateSession(session.id, { pinned: !session.pinned_at });
      setSessions((current) => sortSessions(
        current.map((item) => item.id === updated.id ? updated : item),
      ));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "更新置顶状态失败");
    } finally {
      setUpdatingSessionId(undefined);
    }
  }

  return (
    <aside className="sidebar">
      <button
        className="brand-block"
        type="button"
        onClick={onNewSession}
        disabled={disabled}
        aria-label="新建对话"
      >
        <div className="brand-mark" aria-hidden="true"><Waves size={21} /></div>
        <div>
          <strong>Tara Agent</strong>
          <span>海洋数据分析</span>
        </div>
      </button>

      <nav className="primary-nav" aria-label="主要导航">
        <button type="button" onClick={onNewSession} disabled={disabled}>
          <Plus size={16} aria-hidden="true" />
          新建对话
        </button>
      </nav>

      <section className="session-history" aria-label="对话记录">
        <div className="session-heading">
          <p className="sidebar-label">对话记录</p>
          <div>
            {loading ? <LoaderCircle className="spin" size={13} aria-label="正在加载" /> : null}
            {!loading && sessions.length > 0 ? (
              <button type="button" onClick={toggleManaging} disabled={disabled || deleting}>
                {managing ? "完成" : "管理"}
              </button>
            ) : null}
          </div>
        </div>

        {error ? (
          <div className="session-feedback">
            <span>{error}</span>
            <button
              type="button"
              onClick={() => {
                setLoading(true);
                setError(undefined);
                setRetryVersion((value) => value + 1);
              }}
            >
              <RefreshCw size={13} />重新加载
            </button>
          </div>
        ) : null}

        {!loading && !error && sessions.length === 0 ? (
          <div className="session-empty">
            <MessageSquare size={18} aria-hidden="true" />
            <strong>暂无历史对话</strong>
            <span>完成一次分析后将显示在这里</span>
          </div>
        ) : null}

        {managing && sessions.length > 0 ? (
          <div className="session-selection-toolbar">
            <button
              type="button"
              onClick={toggleAllSessions}
              disabled={disabled || deleting}
              aria-pressed={selectedIds.size === sessions.length}
            >
              <Check size={13} aria-hidden="true" />
              {selectedIds.size === sessions.length ? "取消全选" : "全选"}
            </button>
            <span>已选 {selectedIds.size} 个</span>
          </div>
        ) : null}

        {sessions.length > 0 ? (
          <div className="session-list">
            {sessions.some((session) => session.pinned_at) ? (
              <p className="session-group-label pinned">置顶</p>
            ) : null}
            {sessions.some((session) => !session.pinned_at) ? (
              <p className="session-group-label recent">最近</p>
            ) : null}
            {sessions.map((session) => managing ? (
              <label
                key={session.id}
                className={`session-select-row ${sessionGroupClass(session)}${selectedIds.has(session.id) ? " selected" : ""}`}
              >
                <input
                  type="checkbox"
                  checked={selectedIds.has(session.id)}
                  onChange={() => toggleSession(session.id)}
                  disabled={disabled || deleting}
                />
                <span>
                  <strong className="session-title">
                    {session.pinned_at ? <Pin size={11} aria-label="已置顶" /> : null}
                    <span>{session.title ?? "未命名对话"}</span>
                  </strong>
                  <time dateTime={session.updated_at}>{formatDateTime(session.updated_at)}</time>
                </span>
              </label>
            ) : editingSessionId === session.id ? (
              <form
                key={session.id}
                className={`session-rename-form ${sessionGroupClass(session)}`}
                onSubmit={renameSession}
              >
                <input
                  value={titleDraft}
                  onChange={(event) => setTitleDraft(event.target.value)}
                  maxLength={80}
                  aria-label="会话名称"
                  autoFocus
                  disabled={updatingSessionId === session.id}
                />
                <button
                  type="submit"
                  aria-label="保存名称"
                  title="保存"
                  disabled={!titleDraft.trim() || updatingSessionId === session.id}
                >
                  {updatingSessionId === session.id
                    ? <LoaderCircle className="spin" size={14} />
                    : <Check size={14} />}
                </button>
                <button
                  type="button"
                  aria-label="取消重命名"
                  title="取消"
                  onClick={() => setEditingSessionId(undefined)}
                  disabled={updatingSessionId === session.id}
                >
                  <X size={14} />
                </button>
              </form>
            ) : (
              <div
                key={session.id}
                ref={menuSessionId === session.id ? menuRef : undefined}
                className={`session-item ${sessionGroupClass(session)}${session.id === activeSessionId ? " active" : ""}${menuSessionId === session.id ? " menu-open" : ""}`}
              >
                <div className="session-row">
                  <button
                    className="session-open"
                    type="button"
                    onClick={() => onSelectSession(session.id)}
                    disabled={disabled || deleting || updatingSessionId === session.id}
                    aria-current={session.id === activeSessionId ? "page" : undefined}
                  >
                    <strong className="session-title">
                      {session.pinned_at ? <Pin size={11} aria-label="已置顶" /> : null}
                      <span>{session.title ?? "未命名对话"}</span>
                    </strong>
                    <time dateTime={session.updated_at}>{formatDateTime(session.updated_at)}</time>
                  </button>
                  <button
                    className="session-more"
                    type="button"
                    aria-label="会话操作"
                    title="会话操作"
                    aria-expanded={menuSessionId === session.id}
                    onClick={() => setMenuSessionId((current) => (
                      current === session.id ? undefined : session.id
                    ))}
                    disabled={disabled || deleting || updatingSessionId === session.id}
                  >
                    {updatingSessionId === session.id
                      ? <LoaderCircle className="spin" size={15} />
                      : <MoreHorizontal size={16} />}
                  </button>
                </div>
                {menuSessionId === session.id ? (
                  <div className="session-menu" role="menu">
                    <button type="button" role="menuitem" onClick={() => startRenaming(session)}>
                      <Pencil size={14} />重命名
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => void togglePinned(session)}
                    >
                      {session.pinned_at ? <PinOff size={14} /> : <Pin size={14} />}
                      {session.pinned_at ? "取消置顶" : "置顶"}
                    </button>
                    <button
                      className="danger"
                      type="button"
                      role="menuitem"
                      onClick={() => requestSessionDeletion(session)}
                    >
                      <Trash2 size={14} />删除
                    </button>
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}

        {managing ? (
          <button
            className="session-delete-button"
            type="button"
            onClick={requestSelectedSessionsDeletion}
            disabled={selectedIds.size === 0 || disabled || deleting}
          >
            {deleting ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}
            删除{selectedIds.size > 0 ? ` ${selectedIds.size}` : ""}
          </button>
        ) : null}
      </section>

      <div className={`sidebar-account${user.is_guest ? " guest" : ""}`}>
        <div>
          <strong>{user.display_name}</strong>
          {!user.is_guest ? <span>{user.email}</span> : null}
        </div>
        <button
          type="button"
          onClick={() => setSignOutRequested(true)}
          aria-label="退出登录"
          title="退出登录"
        >
          <LogOut size={15} />
        </button>
      </div>

      {signOutRequested ? (
        <ConfirmationDialog
          titleId="sign-out-dialog-title"
          title="退出登录？"
          description="退出后将返回登录页面，当前账户中的会话数据不会被删除。"
          confirmLabel="退出登录"
          confirmStyle="primary"
          busy={signingOut}
          onCancel={() => setSignOutRequested(false)}
          onConfirm={() => void confirmSignOut()}
        />
      ) : null}

      {deleteRequest ? (
        <ConfirmationDialog
          titleId="delete-dialog-title"
          title={deleteRequest.sessionIds.length === 1 ? "删除对话？" : "删除多个对话？"}
          description={deleteRequest.sessionIds.length === 1 ? (
            <>这会永久删除 <strong>“{deleteRequest.title}”</strong> 及其全部分析记录。</>
          ) : (
            <>这会永久删除选中的 <strong>{deleteRequest.sessionIds.length} 个对话</strong> 及其全部分析记录。</>
          )}
          note="删除后无法恢复。"
          confirmLabel="删除"
          confirmStyle="danger"
          busy={deleting}
          error={deleteError}
          onCancel={() => {
            setDeleteRequest(undefined);
            setDeleteError(undefined);
          }}
          onConfirm={() => void confirmDeletion()}
        />
      ) : null}
    </aside>
  );
}

type ConfirmationDialogProps = {
  titleId: string;
  title: string;
  description: ReactNode;
  note?: string;
  confirmLabel: string;
  confirmStyle: "primary" | "danger";
  busy: boolean;
  error?: string;
  onCancel: () => void;
  onConfirm: () => void;
};

function ConfirmationDialog({
  titleId,
  title,
  description,
  note,
  confirmLabel,
  confirmStyle,
  busy,
  error,
  onCancel,
  onConfirm,
}: ConfirmationDialogProps) {
  return (
    <div
      className="confirmation-dialog-layer"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) {
          onCancel();
        }
      }}
    >
      <section
        className="confirmation-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <h2 id={titleId}>{title}</h2>
        <p>{description}</p>
        {note ? <span>{note}</span> : null}
        {error ? <div className="confirmation-dialog-error">{error}</div> : null}
        <footer>
          <button type="button" onClick={onCancel} disabled={busy} autoFocus>
            取消
          </button>
          <button className={confirmStyle} type="button" onClick={onConfirm} disabled={busy}>
            {busy ? <LoaderCircle className="spin" size={15} /> : null}
            {confirmLabel}
          </button>
        </footer>
      </section>
    </div>
  );
}

function sortSessions(sessions: SessionSummary[]): SessionSummary[] {
  return [...sessions].sort((left, right) => {
    if (left.pinned_at && right.pinned_at) {
      return right.pinned_at.localeCompare(left.pinned_at);
    }
    if (left.pinned_at) {
      return -1;
    }
    if (right.pinned_at) {
      return 1;
    }
    return right.updated_at.localeCompare(left.updated_at);
  });
}

function sessionGroupClass(session: SessionSummary): string {
  return session.pinned_at ? "session-group-pinned" : "session-group-recent";
}
