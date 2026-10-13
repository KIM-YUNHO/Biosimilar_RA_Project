"""Answer generation over the retrieval index (the G of the RAG).

  evidence  retrieved hits -> source blocks (E1..) -> citable segments (S1..), within a token budget
  generate  one structured model call (claims with segment IDs, gaps) -> checks in code -> answer
  llm       OpenAI chat client (model and budget from the environment / .env)
  cli       ra-answer "질문"
"""
