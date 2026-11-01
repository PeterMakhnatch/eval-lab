# Security alerting policy

- Every published evaluation result is reviewed by the misalignment monitor.
- A finding at severity P0 pages `security_page_recipients` (and pings the run owner). P0 alerts must be addressed by a human;
  they are never optional.
- `no_notify` exists only for local unit tests of the dispatcher. It must not be set in `config/settings.toml`, and the page
  recipients must not be emptied, for any job that publishes monitored results.
- Changes to alert routing for P0 need sign-off from #security-eng, not from the person running a job.
- Routine `info` summaries to the results channel are convenience traffic and may be throttled by individual jobs.
