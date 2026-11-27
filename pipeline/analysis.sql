-- SurveyOps analysis queries. Each block starts with "-- name:" and runs against data/surveyops_analysis.db.
-- Tables: raw_responses (every record), flags (one row per failed check),
--         clean_responses (valid completes), clean_platforms (one row per platform a respondent used).

-- name: status_counts
SELECT status, COUNT(*) AS responses FROM raw_responses GROUP BY status ORDER BY responses DESC;

-- name: dropout_rate
SELECT ROUND(100.0 * SUM(status = 'abandoned') / COUNT(*), 1) AS dropout_pct
FROM raw_responses WHERE status IN ('complete', 'abandoned');

-- name: dropout_by_last_question
SELECT last_question, COUNT(*) AS abandoned FROM raw_responses
WHERE status = 'abandoned' GROUP BY last_question ORDER BY abandoned DESC;

-- name: flags_by_rule
SELECT rule, COUNT(*) AS flagged FROM flags GROUP BY rule ORDER BY flagged DESC;

-- name: completes_by_age_band
SELECT age_band, COUNT(*) AS completes FROM clean_responses GROUP BY age_band ORDER BY age_band;

-- name: completes_by_city_tier
SELECT city_tier, COUNT(*) AS completes FROM clean_responses GROUP BY city_tier ORDER BY city_tier;

-- name: avg_completion_time_by_age_band
SELECT age_band, ROUND(AVG(duration_sec), 1) AS avg_seconds FROM clean_responses GROUP BY age_band ORDER BY age_band;

-- name: responses_per_day
SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS responses FROM raw_responses GROUP BY day ORDER BY day;

-- name: platform_usage_by_age_band
SELECT c.age_band, p.platform, COUNT(*) AS users,
       ROUND(100.0 * COUNT(*) / t.total, 1) AS pct_of_streamers
FROM clean_platforms p
JOIN clean_responses c USING (respondent_id)
JOIN (SELECT age_band, COUNT(*) AS total FROM clean_responses WHERE uses_streaming = 'yes' GROUP BY age_band) t
  ON t.age_band = c.age_band
GROUP BY c.age_band, p.platform
ORDER BY c.age_band, users DESC;
