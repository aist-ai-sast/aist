SELECT json_build_object(
  'findings', (SELECT count(*) FROM dojo_finding),
  'finding_hashes', (SELECT md5(string_agg(id::text || ':' || coalesce(hash_code, ''), '|' ORDER BY id)) FROM dojo_finding),
  'metadata', (SELECT count(*) FROM dojo_dojometa),
  'source_links', (SELECT count(*) FROM dojo_dojometa WHERE name = 'sourcefile_link'),
  'pipelines', (SELECT count(*) FROM aist_aistpipeline),
  'nonterminal_pipelines', (SELECT count(*) FROM aist_aistpipeline WHERE status NOT IN ('FINISHED', 'FINISHED_WITH_WARNINGS')),
  'execution_leases', (SELECT count(*) FROM aist_pipelineexecutionlease WHERE released_at IS NULL),
  'ai_responses', (SELECT count(*) FROM aist_aistaifindingresponse),
  'projects', (SELECT count(*) FROM aist_aistproject),
  'project_versions', (SELECT count(*) FROM aist_aistprojectversion)
);
