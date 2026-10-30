<?php
// Dump or restore one web app's database, as the app's owner.
//
// site-update (root) runs this through sudo -u <owner> with php -n, so it reads the app's own
// database settings with the owner's rights and root never needs a database password. The
// password reaches the MariaDB client through MYSQL_PWD (readable only by the owner and root),
// never a command line. The dump goes to stdout; a restore reads it from stdin and first drops
// every table and view, so tables a failed update created do not survive the rollback.
//
// php -n appdb.php dump|import env|php <config file> <keys JSON> <expected database name>

declare(strict_types=1);

function fail(int $code): never
{
    exit($code);
}

/** Laravel-style .env: KEY=value, KEY="value" or KEY='value'. */
function env_settings(string $file): array
{
    $values = [];
    foreach (file($file, FILE_IGNORE_NEW_LINES) ?: [] as $line) {
        if (!preg_match('/^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$/', $line, $m)) {
            continue;
        }
        $value = $m[2];
        if (preg_match('/^"((?:[^"\\\\]|\\\\.)*)"/', $value, $q)) {
            $value = stripcslashes($q[1]);
        } elseif (preg_match("/^'([^']*)'/", $value, $q)) {
            $value = $q[1];
        } else {
            $value = preg_replace('/\s+#.*$/', '', $value);
        }
        $values[$m[1]] = $value;
    }
    return [
        'host' => $values['DB_HOST'] ?? '127.0.0.1',
        'port' => $values['DB_PORT'] ?? '',
        'socket' => $values['DB_SOCKET'] ?? '',
        'name' => $values['DB_DATABASE'] ?? '',
        'user' => $values['DB_USERNAME'] ?? '',
        'password' => $values['DB_PASSWORD'] ?? '',
    ];
}

/** A PHP file that returns an array; $keys maps host, port, name, user, password to its keys. */
function php_settings(string $file, array $keys): array
{
    $config = (static fn (string $path) => require $path)($file);
    if (!is_array($config)) {
        fail(3);
    }
    $settings = [];
    foreach (['host', 'port', 'socket', 'name', 'user', 'password'] as $key) {
        $settings[$key] = isset($keys[$key]) ? (string) ($config[$keys[$key]] ?? '') : '';
    }
    return $settings;
}

function client(string $name): string
{
    foreach (["/usr/bin/mariadb$name", "/usr/bin/mysql$name"] as $path) {
        if (is_executable($path)) {
            return $path;
        }
    }
    fail(4);
}

function run(array $command, array $settings, $stdin, $stdout, ?string $prefix = null): int
{
    $env = ['PATH' => '/usr/bin:/bin', 'LANG' => 'C.UTF-8', 'MYSQL_PWD' => $settings['password']];
    $process = proc_open($command, [0 => $prefix === null ? $stdin : ['pipe', 'r'], 1 => $stdout,
        2 => ['file', '/dev/null', 'w']], $pipes, '/', $env);
    if (!is_resource($process)) {
        fail(5);
    }
    if ($prefix !== null) {
        fwrite($pipes[0], $prefix);
        while (!feof($stdin)) {
            $chunk = fread($stdin, 1 << 20);
            if ($chunk === false || fwrite($pipes[0], $chunk) === false) {
                break;
            }
        }
        fclose($pipes[0]);
    }
    return proc_close($process);
}

[, $action, $format, $file, $keysJson, $expected] = array_pad($argv, 6, '');
$keys = json_decode($keysJson, true);
if (!in_array($action, ['dump', 'import'], true) || !is_array($keys) || !is_file($file)
    || !preg_match('/^[A-Za-z0-9_]{1,64}$/', $expected)) {
    fail(2);
}
$settings = $format === 'env' ? env_settings($file) : ($format === 'php' ? php_settings($file, $keys) : fail(2));
if ($settings['name'] !== $expected || $settings['user'] === '') {
    fail(3); // the app points at another database than site-update was configured for
}
$connection = ['--user=' . $settings['user'], '--default-character-set=utf8mb4'];
if ($settings['socket'] !== '') {
    $connection[] = '--socket=' . $settings['socket'];
} else {
    $connection[] = '--host=' . ($settings['host'] !== '' ? $settings['host'] : '127.0.0.1');
    if ($settings['port'] !== '') {
        $connection[] = '--port=' . (int) $settings['port'];
    }
}

if ($action === 'dump') {
    exit(run(array_merge([client('-dump')], $connection, ['--single-transaction', '--quick', '--no-tablespaces',
        '--hex-blob', '--routines', '--triggers', '--', $expected]), $settings, ['file', '/dev/null', 'r'], STDOUT));
}

// Drop what is there now, then load the dump, in one client session.
$list = fopen('php://temp', 'w+');
$query = "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = DATABASE()";
if (run(array_merge([client('')], $connection, ['--batch', '--skip-column-names', '-e', $query, '--', $expected]),
        $settings, ['file', '/dev/null', 'r'], $list) !== 0) {
    fail(6);
}
rewind($list);
$prefix = "SET FOREIGN_KEY_CHECKS=0;\n";
while (($line = fgets($list)) !== false) {
    [$table, $type] = array_pad(explode("\t", rtrim($line, "\n")), 2, '');
    $prefix .= ($type === 'VIEW' ? 'DROP VIEW IF EXISTS ' : 'DROP TABLE IF EXISTS ') . '`' . str_replace('`', '``', $table) . "`;\n";
}
exit(run(array_merge([client('')], $connection, ['--', $expected]), $settings, STDIN, STDOUT, $prefix));
