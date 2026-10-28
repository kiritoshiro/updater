<?php
/*
 * Test fixture only: simulates an update that breaks the front end. When the marker file
 * wp-content/break-after-update names "plugin:slug:version" or "core::version", any web request
 * fails with a fatal error once that component is newer than the given version.
 */
if ( defined( 'WP_CLI' ) || ! is_readable( WP_CONTENT_DIR . '/break-after-update' ) ) {
	return;
}
list( $kind, $slug, $version ) = explode( ':', trim( file_get_contents( WP_CONTENT_DIR . '/break-after-update' ) ) );
if ( 'core' === $kind ) {
	$installed = $GLOBALS['wp_version'];
} else {
	$data      = get_file_data( WP_PLUGIN_DIR . "/$slug/$slug.php", array( 'Version' => 'Version' ) );
	$installed = $data['Version'];
}
if ( version_compare( $installed, $version, '>' ) ) {
	throw new Error( 'Simulated broken update' );
}
