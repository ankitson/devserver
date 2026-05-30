with daily_summaries as (
    -- Step 1: Collapse all individual sessions into one row per day/game
    select
        date_trunc('day', date_session) as dt,
        game_name,
        sum(elapsed_seconds) as daily_seconds
    from playnite_sessions
    group by date_trunc('day', date_session), game_name
)
-- Step 2: Calculate both the standalone daily hours and the continuous all-time total
select
    dt,
    game_name,
    -- Simple total for just this day
    round(daily_seconds / (60.0 * 60.0), 1) as daily_hours,

    -- Continuous running total across days (ordered chronologically so it builds up)
    round(
        sum(daily_seconds) over (
            partition by game_name
            order by dt
            rows between unbounded preceding and current row
        ) / (60.0 * 60.0),
        4
    ) as continuous_all_time_hours
from daily_summaries
-- Final display sorted newest to oldest
order by dt desc, game_name;