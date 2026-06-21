-- Add phone lookup columns to patients table
ALTER TABLE patients ADD COLUMN IF NOT EXISTS phone_type   text default 'unknown';
ALTER TABLE patients ADD COLUMN IF NOT EXISTS phone2       text;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS phone2_type  text;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS sms_capable  boolean;
ALTER TABLE patients ADD COLUMN IF NOT EXISTS sms_number   text;
