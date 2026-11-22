CREATE TABLE IF NOT EXISTS analysis_batches (
  id INT AUTO_INCREMENT PRIMARY KEY,
  service ENUM('deep_clean','appliance_clean') NOT NULL,
  sample_count INT NOT NULL,
  checksum CHAR(64) NOT NULL,
  sample_rows JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY service_checksum (service,checksum)
) CHARACTER SET utf8mb4;
