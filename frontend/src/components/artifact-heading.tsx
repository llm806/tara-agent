export function ArtifactHeading({ title, scope, sources }: {
  title: string; scope: string; sources: string[];
}) {
  return (
    <span className="artifact-heading">
      <span className="artifact-heading-title">{title}</span>
      {scope ? <span className="artifact-heading-scope">{scope}</span> : null}
      <span className="artifact-heading-sources">
        <span>数据来源</span>
        {sources.length ? sources.map((source) => <span className="artifact-source" key={source}>{source}</span>) : <span>未记录</span>}
      </span>
    </span>
  );
}
