<?php

declare(strict_types=1);

$routes = file('/proc/net/route', FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
if ($routes === false) {
    fwrite(STDERR, "could not inspect install-hook network routes\n");
    exit(2);
}
foreach (array_slice($routes, 1) as $route) {
    $columns = preg_split('/\s+/', trim($route));
    if (is_array($columns) && ($columns[1] ?? '') === '00000000') {
        fwrite(STDERR, "install hook ran with a default network route\n");
        exit(3);
    }
}

file_put_contents('offline-install-hook.proof', "offline\n");
