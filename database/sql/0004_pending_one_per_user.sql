-- يمنع وجود أكثر من طلب واحد لنفس المستخدم في pending_requests
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM   pg_indexes
    WHERE  schemaname = 'public'
    AND    indexname = 'ux_pending_one_per_user'
  ) THEN
    CREATE UNIQUE INDEX ux_pending_one_per_user ON public.pending_requests(user_id);
  END IF;
END $$;
