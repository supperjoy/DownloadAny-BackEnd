ALTER TABLE feedbacks ADD COLUMN email VARCHAR(320) NULL;
ALTER TABLE feedbacks ADD COLUMN ip_address VARCHAR(64) NOT NULL DEFAULT '';
CREATE INDEX ix_feedbacks_ip_address ON feedbacks (ip_address);
