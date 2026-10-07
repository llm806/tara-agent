export function ArtifactHeading({ title, scope, sources }: {
  title: string; scope: string; sources: string[];
}) {
  return (
    <span className="artifact-heading">
      <span className="artifact-heading-title">{title}</span>
      {scope ? <span className="artifact-heading-scope">{scope}</span> : null}
      <DataSourceList sources={sources} />
    </span>
  );
}
import { sourceGroups } from "@/lib/trace-data-summary";

export function DataSourceList({ sources }: { sources: string[] }) {
  const groups = sourceGroups(sources);
  return (
    <span className="source-groups">
      {groups.length ? groups.map((group) => (
        <span className="source-files" key={group.label}>
          <span className="source-files-label">{group.label}（{group.files.length}）</span>
          <span className="source-files-list">
            {group.files.map((name) => <code key={name}>{name}</code>)}
          </span>
        </span>
      )) : <span>数据来源未记录</span>}
    </span>
  );
}
