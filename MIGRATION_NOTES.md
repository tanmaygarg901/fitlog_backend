## Dashboard migration notes

Run this SQL in Supabase before using dashboard goal targets:

```sql
create table user_goals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) unique,
  calories integer default 2500,
  protein_g integer default 180,
  carbs_g integer default 250,
  fat_g integer default 70,
  workouts_per_week integer default 4,
  updated_at timestamptz default now()
);
```
