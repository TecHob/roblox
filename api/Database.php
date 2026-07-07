<?php
/**
 * Database — Singleton PDO wrapper
 */
class Database
{
    private static ?PDO $instance = null;

    public static function get(): PDO
    {
        if (self::$instance === null) {
            $dsn = 'mysql:host=' . DB_HOST . ';dbname=' . DB_NAME . ';charset=' . DB_CHARSET;
            self::$instance = new PDO($dsn, DB_USER, DB_PASS, [
                PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES   => false,
                PDO::MYSQL_ATTR_INIT_COMMAND => 'SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci',
            ]);
        }
        return self::$instance;
    }

    /**
     * Executar query preparada e retornar statement
     */
    public static function query(string $sql, array $params = []): PDOStatement
    {
        $stmt = self::get()->prepare($sql);
        $stmt->execute($params);
        return $stmt;
    }

    /**
     * Buscar todas as linhas
     */
    public static function fetchAll(string $sql, array $params = []): array
    {
        return self::query($sql, $params)->fetchAll();
    }

    /**
     * Buscar uma linha
     */
    public static function fetchOne(string $sql, array $params = []): ?array
    {
        $row = self::query($sql, $params)->fetch();
        return $row ?: null;
    }

    /**
     * Insert e retornar ID
     */
    public static function insert(string $table, array $data): int
    {
        $cols = implode(', ', array_map(fn($c) => "`$c`", array_keys($data)));
        $placeholders = implode(', ', array_fill(0, count($data), '?'));
        $sql = "INSERT INTO `$table` ($cols) VALUES ($placeholders)";
        self::query($sql, array_values($data));
        return (int) self::get()->lastInsertId();
    }

    /**
     * Update por ID
     */
    public static function update(string $table, int $id, array $data): int
    {
        $sets = implode(', ', array_map(fn($c) => "`$c` = ?", array_keys($data)));
        $sql = "UPDATE `$table` SET $sets WHERE `id` = ?";
        $params = array_values($data);
        $params[] = $id;
        return self::query($sql, $params)->rowCount();
    }

    /**
     * Delete por ID
     */
    public static function delete(string $table, int $id): int
    {
        return self::query("DELETE FROM `$table` WHERE `id` = ?", [$id])->rowCount();
    }
}
